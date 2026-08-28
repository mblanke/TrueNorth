#!/usr/bin/env bash
# TrueNorth Range — Definition of Done gate.
#
# This script is cited as the truth gate ("Agents talk; DoD decides", docs/AGENTS.md).
# Until 2026-08-19 it could not fail and did not run: `has ruff` / `has pytest` were
# both false because those tools live in .venv/bin and not on PATH, so the entire
# Python block was skipped before the trailing `|| true` ever mattered. It then fell
# into an Angular block that dies on a missing ChromeHeadless. Vacuous and unrunnable
# at the same time, which is why nobody ran it.
#
# Rules now: a missing tool is a FAILURE, never a silent skip. No `|| true` anywhere.
set -euo pipefail
echo "== TrueNorth Range DoD Gate =="
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
fail() { echo "DoD FAIL: $1" >&2; exit 1; }
run() { echo "+ $*"; "$@"; }

# Use the venv explicitly. PY can be overridden for CI images that install globally.
PY="${PY:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || fail "no python at $PY (set PY=... to override)"
"$PY" -m ruff --version >/dev/null 2>&1 || fail "ruff not installed in $PY"
"$PY" -m pytest --version >/dev/null 2>&1 || fail "pytest not installed in $PY"

PY_TARGETS=(control-plane/ scenario-engine/ ai-orchestrator/ telemetry/ tools/ tests/)

# Ruff, as a ratchet rather than a cliff. The repo carries pre-existing lint debt; a
# gate that fails on all of it on day one gets switched off on day two, which is the
# failure mode this rewrite exists to end. So:
#   - breakage-class rules (undefined name, redefinition, syntax) ALWAYS fail;
#   - everything else may not get WORSE than the recorded baseline.
BASELINE_FILE="$ROOT/.dod-ruff-baseline"

echo "+ ruff check (breakage rules: F821,F811,E9)"
if ! "$PY" -m ruff check "${PY_TARGETS[@]}" --exclude="control-plane/web" --no-fix \
        --select F821,F811,E9 --output-format=concise; then
  fail "undefined names / redefinitions / syntax errors — these are real bugs, not style"
fi

CUR=$("$PY" -m ruff check "${PY_TARGETS[@]}" --exclude="control-plane/web" --no-fix \
        --output-format=concise 2>/dev/null | grep -c ':' || true)
BASE=$(cat "$BASELINE_FILE" 2>/dev/null || echo "$CUR")
echo "+ ruff debt: $CUR (baseline $BASE)"
if (( CUR > BASE )); then
  "$PY" -m ruff check "${PY_TARGETS[@]}" --exclude="control-plane/web" --no-fix --output-format=concise || true
  fail "ruff findings rose from $BASE to $CUR — fix the new ones (or lower the baseline deliberately)"
fi
# Ratchet down automatically when the count improves, so cleanup sticks.
if (( CUR < BASE )); then echo "  ratcheting baseline down: $BASE -> $CUR"; fi
echo "$CUR" > "$BASELINE_FILE"

# Match .github/workflows/ci.yml:37 exactly, so local green and CI green mean the same
# thing. tests/integration needs OpenSearch and live provisioners and is a separate CI
# job (ci.yml:137); running it here just produces errors that teach people to ignore
# this script.
run "$PY" -m pytest tests/ --tb=short -q --ignore=tests/integration

# Angular is opt-in until the repo actually carries a karma.conf.js and CHROME_BIN is
# set (see .claude/agents/test-engineer.md). Leaving an unrunnable step in the default
# path is what got this whole script abandoned; make it explicit instead.
if [[ "${DOD_WEB:-0}" == "1" ]]; then
  [[ -f control-plane/web/angular.json ]] || fail "DOD_WEB=1 but no control-plane/web/angular.json"
  pushd control-plane/web >/dev/null
  run npx ng lint
  run npx ng build --configuration=production
  run npx ng test --watch=false --browsers=ChromeHeadless
  popd >/dev/null
else
  echo "  (web checks skipped; set DOD_WEB=1 to include them)"
fi

# Record WHICH STATE passed, so other controls can ask "was DoD green for this exact
# state?" rather than "was DoD ever green?". The Stop hook reads this.
#
# NOT `git write-tree`: that hashes the INDEX, so an unstaged edit leaves it unchanged.
# The 2026-08-19 damage was entirely unstaged, so an index hash would have been blind to
# precisely the case this marker exists for. HEAD + a hash of the full diff against HEAD
# captures staged and unstaged changes to tracked files alike.
{
  printf '%s:%s\n' \
    "$(git rev-parse HEAD 2>/dev/null || echo nohead)" \
    "$(git diff HEAD 2>/dev/null | sha256sum | cut -c1-16)"
  date -u +%FT%TZ
} > "$ROOT/.dod-pass"

echo "DoD PASS"
