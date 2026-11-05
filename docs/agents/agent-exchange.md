# TrueNorth agent exchange

The exchange gives Claude, Codex, and Taz one durable account of work. Taz produces
bounded candidates; Claude reviews implementation and integration; Codex performs an
independent risk and regression review; CI supplies the final machine evidence; the owner
merges. A worker's completion claim is never proof.

The CLI is `.coord/coord.py` (shown as `coord` below). Mutable state, packages, evidence,
and the VPN profile stay under `.coord/` and are gitignored. The task-card and result
formats are tracked and reviewable.

## State and authority

```text
draft -> oracle-ready -> dispatched -> candidate-returned -> guard-green
      -> independent-review -> ci-pending -> ci-green -> merged
```

Any active state can become `blocked` or `rejected`. A rejected candidate returns to
`oracle-ready` only after its card or implementation is corrected. A Taz result can never
skip Claude review, Codex review, or CI. Only `--actor owner` can record `merged`.

The broker limits the review queue to three candidates by default. `coord card ready`
returns no additional work while reviewers are behind. Eight fast patches waiting for
review are inventory, not progress.

## One-time Mac setup

After the branch is merged into the main checkout:

```bash
coord() { python3 "$(git rev-parse --git-common-dir)/../.coord/coord.py" "$@"; }
coord init
coord bridge init \
  --target taz-exchange@TAZ_VPN_HOST \
  --remote-root /srv/truenorth-agent-exchange
coord bridge probe
```

Use the VPN hostname or address that already reaches Taz. SSH authentication comes from
the Mac's SSH agent/config. The generated `.coord/transport.local.json` is mode 0600 and
gitignored; do not put a password, private key, or bearer token in it. `bridge probe`
creates only `inbox`, `outbox`, and `processed` below the configured remote root.

The repository records Taz's local LiteLLM/Claude setup, but has no verified Mac-to-Taz
host mapping. Do not copy the old August endpoint into the profile: confirm the current
VPN route and hostname first.

## Card creation and dispatch

A card is JSON. `oracle_tests` lists protected test files; `oracle_commands` lists the
exact commands that exercise them. Commit the oracle before setting `oracle-ready` and
pin its base commit.

```json
{
  "id": "TN-443-01",
  "issue_number": 443,
  "title": "Reproduce tenant range-output exposure",
  "state": "oracle-ready",
  "risk": "high",
  "autonomy": "candidate-only",
  "review_policy": "dual",
  "base_ref": "taz-base/443-01",
  "base_commit": "FULL_40_CHARACTER_SHA",
  "allowed_paths": ["tests/api/test_ranges.py"],
  "oracle_tests": ["tests/api/test_ranges.py"],
  "oracle_commands": [".venv/bin/python -m pytest tests/api/test_ranges.py -q"],
  "required_checks": ["bash scripts/dod.sh"],
  "diff_budget": 150
}
```

```bash
coord card import /path/to/TN-443-01.json --json
coord card ready --json
coord card export TN-443-01 .coord/outbox --archive --json
coord bridge push .coord/outbox/TN-443-01-FULL_40_CHAR.tar.gz
```

Export changes the card to `dispatched`. The archive includes `card-v2.json`, worker
instructions, and checksums. Taz works from a disposable clone at the exact base commit.
It has no GitHub token, production credential, or authority to push or merge.

## Taz-side mailbox

The SSH account should be unprivileged and restricted to the exchange root. The model
runner reads an archive only after the `.partial` suffix is atomically removed. It verifies
`checksums.sha256`, checks out the exact base in a disposable worktree, and sets `TAZ_CARD`
for the existing write gate.

The runner, outside the model process, creates these five return files:

| File | Producer | Evidence |
|---|---|---|
| `candidate.patch` | runner from `git diff --binary` | proposed changes |
| `result-v2.json` | runner plus model report | identity, base, files, summary, self-review |
| `test-results.json` | runner | exact commands, exit codes, duration, bounded output |
| `diff-stat.txt` | runner | human-readable size |
| `taz-guard.json` | runner | guard result and served model observed from the backend |

The runner must query the serving backend for its actual model id. A LiteLLM alias is not
an identity. `result-v2.json` and `taz-guard.json` must agree on that id.

After the bounded Claude/GLM session stops, the supervisor writes two small files outside
the candidate worktree:

```json
{"id":"glm-5.3","runtime":"vllm","source":"supervisor /v1/models probe"}
```

```json
{"summary":"What changed","limitations":[],"confidence":0.72}
```

It then runs the tracked packager. The packager verifies the input checksums and base
commit, runs the exact oracle commands, makes a local-only candidate commit, runs the
existing `taz_guard.py`, records bounded output, and creates the return archive. It never
pushes.

```bash
python3 tools/taz/exchange_result.py \
  --card /srv/truenorth-agent-exchange/work/TN-443-01/card-v2.json \
  --repo /srv/truenorth-agent-exchange/work/TN-443-01/repo \
  --output /srv/truenorth-agent-exchange/outbox/TN-443-01-result \
  --served-model-json /run/taz/TN-443-01-served-model.json \
  --self-review-json /run/taz/TN-443-01-self-review.json \
  --worker-id taz-glm-session-20261105T1900Z \
  --archive
```

Minimum `result-v2.json`:

```json
{
  "schema_version": 2,
  "card_id": "TN-443-01",
  "worker": "taz-glm-session-20261105T1900Z",
  "served_model": {"id": "glm-5.3", "runtime": "vllm"},
  "base_commit": "FULL_40_CHARACTER_SHA",
  "head_commit": null,
  "patch_sha256": "SHA256_OF_CANDIDATE_PATCH",
  "files_changed": ["tests/api/test_ranges.py"],
  "summary": "Added a failing cross-tenant response test.",
  "self_review": {
    "limitations": ["Did not exercise a live vCenter"],
    "confidence": 0.72
  }
}
```

The result directory is archived as `<card-id>-result.tar.gz` in the remote `outbox`.
Taz completion means only “candidate returned.”

## Pull, ingest, and review

```bash
coord bridge pull TN-443-01-result.tar.gz .coord/inbox
# Extract into .coord/inbox/TN-443-01-result, then:
coord card ingest TN-443-01 .coord/inbox/TN-443-01-result --json
coord card show TN-443-01
```

Ingest preserves evidence, recomputes the patch hash and changed paths, enforces the diff
budget and allowed paths, requires every exact oracle command to have exit code 0, and
compares the runner-observed model with the manifest. Invalid candidates are retained as
rejected attempts so quality metrics cannot hide them.

Claude reviews first:

```bash
coord card review TN-443-01 --reviewer claude --decision approve \
  --note "Reviewed patch and test intent; no contract or integration regression found."
```

Codex reviews independently against the card, surrounding code, security boundaries,
regression surface, and test quality:

```bash
coord card review TN-443-01 --reviewer codex --decision approve \
  --note "Scope and regression review clean; oracle fails on base and passes on candidate."
```

After the real branch/PR CI run:

```bash
coord card ci TN-443-01 --status green --run-url https://github.example/run/123 --json
coord card set-state TN-443-01 merged --actor owner --note "Merged as PR #456"
```

Green CI requires a run URL. `coord card metrics` reports guard validity, review
rejections, first-pass acceptance, and rework. Those signals decide whether Taz earns
broader cards; elapsed time is not a quality score.

## Claude and Codex consumption

Both agents use the JSON CLI rather than reading SQLite:

```bash
coord card watch --consumer claude
coord card watch --consumer codex
coord card list --state guard-green --json
coord card list --state independent-review --json
```

Each consumer has a durable event cursor. A later MCP wrapper can expose these operations
without changing state or evidence formats. The CLI works on macOS system Python and is
easy to test and audit, so it is the first integration boundary.

## Failure handling

- VPN down: the archive remains in the Mac outbox; retry `bridge push`.
- Taz down: the card stays `dispatched`; mark `blocked` with the observed reason.
- Invalid bundle: ingest records the attempt and rejects it. Fix and re-dispatch.
- Claude requests changes: the card becomes `rejected`; preserve the old evidence.
- Reviewer disagreement: record both notes and leave the card blocked for the owner.
- CI red: record the run; a new candidate begins a new attempt.

Do not use the exchange for production secrets, learner data, classified data, or grade
transfer between enclaves. It is a development-work coordination channel.
