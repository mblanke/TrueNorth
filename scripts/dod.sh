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
# Build it from the same file CI installs, so local green and CI green agree:
#   uv venv .venv --python 3.11
#   uv pip install --python .venv/bin/python -r requirements-test.txt "ruff==0.16.3"
# ruff is pinned because a newer one adds rules and moves .dod-ruff-baseline.
PY="${PY:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || fail "no python at $PY (set PY=... to override)"
"$PY" -m ruff --version >/dev/null 2>&1 || fail "ruff not installed in $PY"
"$PY" -m pytest --version >/dev/null 2>&1 || fail "pytest not installed in $PY (pip install -r requirements-test.txt)"

# Ruff ratchet (breakage rules always fail; other debt may not exceed
# .dod-ruff-baseline). Lives in its own script so CI runs the identical check.
PY="$PY" "$ROOT/scripts/ruff-gate.sh"

# MOSA: modularity debt (raw SQL in the worker, vendor SDKs outside adapters, hardcoded
# backend branches, god-file growth) ratchets like ruff debt. docs/adr/0003.
echo "+ MOSA ratchet (.dod-mosa-baseline)"
"$PY" scripts/mosa_check.py || fail "MOSA modularity debt grew — see the locations above"

# The committed OpenAPI file is the published API contract. Code and contract must
# agree; an intended change is regenerated and committed in the same change. docs/adr/0002.
echo "+ OpenAPI contract drift (docs/interfaces/openapi.json)"
"$PY" scripts/export_openapi.py --check 2>/dev/null \
  || fail "API contract drift — run: $PY scripts/export_openapi.py, review, commit"

# The API -> worker task contract: worker/worker/contracts.py is the source; the API's
# copy and docs/interfaces/worker-tasks.schema.json must match it.
echo "+ Worker task contract drift"
"$PY" scripts/export_task_contracts.py --check \
  || fail "task contract drift — run: $PY scripts/export_task_contracts.py, review, commit"

# Match the test-python job in .github/workflows/ci.yml exactly, so local green and CI
# green mean the same thing (its lint-python job runs scripts/ruff-gate.sh too). tests/integration
# needs OpenSearch and live provisioners and is a separate CI job (`integration`); running
# it here just produces errors that teach people to ignore this script.
# -P: do not put the repo root on sys.path. CI runs plain `pytest`, which does not, so a
# test that imports another test module (`from tests.api...`) must fail here too.
run "$PY" -P -m pytest tests/ --tb=short -q --ignore=tests/integration

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
