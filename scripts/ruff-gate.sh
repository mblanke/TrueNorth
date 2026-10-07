#!/usr/bin/env bash
# Ruff, as a ratchet rather than a cliff. Shared by scripts/dod.sh and CI's lint-python
# job so local green and CI green mean the same thing.
#
# The repo carries pre-existing lint debt; a gate that fails on all of it on day one
# gets switched off on day two, which is the failure mode this exists to end. So:
#   - breakage-class rules (undefined name, redefinition, syntax) ALWAYS fail;
#   - everything else may not get WORSE than the recorded baseline.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PY:-$ROOT/.venv/bin/python}"
fail() { echo "DoD FAIL: $1" >&2; exit 1; }

PY_TARGETS=(control-plane/ scenario-engine/ ai-orchestrator/ telemetry/ tools/ noise-agent/ tests/)
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
