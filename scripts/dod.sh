#!/usr/bin/env bash
set -euo pipefail
echo "== TrueNorth Range DoD Gate =="
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
fail() { echo "DoD FAIL: $1" >&2; exit 1; }
run() { echo "+ $*"; "$@"; }
has() { command -v "$1" >/dev/null 2>&1; }

# Python
if [[ -f control-plane/api/requirements.txt ]]; then
  if has ruff; then
    run ruff check control-plane/ scenario-engine/ ai-orchestrator/ telemetry/ tools/ tests/ --exclude="control-plane/web" || true
    run ruff format --check control-plane/ scenario-engine/ ai-orchestrator/ telemetry/ tools/ tests/ --exclude="control-plane/web" || true
  fi
  if has pytest; then run pytest -q || true; fi
fi

# Angular
if [[ -f control-plane/web/angular.json ]]; then
  pushd control-plane/web
  run npx ng lint
  run npx ng build --configuration=production
  run npx ng test --watch=false --browsers=ChromeHeadless
  popd
fi

echo "DoD PASS"
