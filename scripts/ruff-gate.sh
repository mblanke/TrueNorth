#!/usr/bin/env bash
# Ruff lint and format, as ratchets rather than cliffs. Shared by scripts/dod.sh and CI's lint-python
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

# Formatting, ratcheted per FILE rather than by count. .dod-format-baseline lists the
# files that were unformatted when this started; every other file must already be
# `ruff format`-clean. A count would let a new unformatted file in whenever someone
# formatted an old one. The list only shrinks: a listed file that becomes clean drops
# off, and that edit belongs in the commit. Clear the list by formatting a whole
# directory in a PR of its own; doing it inside feature PRs makes their diffs unreadable.
FORMAT_BASELINE="$ROOT/.dod-format-baseline"
echo "+ ruff format ratchet (.dod-format-baseline)"
UNFORMATTED=$("$PY" -m ruff format --check --output-format=concise "${PY_TARGETS[@]}" \
        --exclude="control-plane/web" 2>/dev/null | grep ': unformatted:' | cut -d: -f1 | sort -u || true)
NEW=$(comm -23 <(printf '%s\n' "$UNFORMATTED" | sed '/^$/d') \
        <(sed '/^$/d' "$FORMAT_BASELINE" 2>/dev/null | sort -u))
if [[ -n "$NEW" ]]; then
  echo "$NEW" | sed 's/^/  unformatted: /'
  fail "files outside .dod-format-baseline need formatting — run: ruff format <file>"
fi
FBASE=$(sed '/^$/d' "$FORMAT_BASELINE" 2>/dev/null | wc -l | tr -d ' ')
printf '%s\n' "$UNFORMATTED" | sed '/^$/d' > "$FORMAT_BASELINE"
FCUR=$(wc -l < "$FORMAT_BASELINE" | tr -d ' ')
echo "+ unformatted files: $FCUR (baseline $FBASE)"
if (( FCUR < FBASE )); then echo "  ratcheting format baseline down: $FBASE -> $FCUR"; fi
