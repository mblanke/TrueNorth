#!/usr/bin/env bash
# Nightly canary: discover -> compile -> check -> build -> drift -> reset -> teardown.
# Run from the repo root on the Ansible control node, as the CI service identity.
#
# Approvals: the CI service account creates its own short-lived approval for CANARY only.
# That is deliberate: the canary is the one range automation may build unattended.
# Non-interactive approval requires TNRANGE_CI_APPROVER=1 AND a readable signing key
# (/etc/truenorth/approval.key or $TNRANGE_APPROVAL_KEY). Give that key to the CI account
# and the human approvers only, never to the account Claude Code runs as.
#
# Exit codes: 0 pass, non-zero fail. Evidence: build/CANARY/{plan,results,manifest,drift,teardown}.*
set -euo pipefail

cd "$(dirname "$0")/.."
R=tools/range
ID=CANARY
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG=build/canary-logs/$STAMP.log
mkdir -p build/canary-logs
exec > >(tee -a "$LOG") 2>&1

: "${TNRANGE_ANSIBLE_ARGS:?set TNRANGE_ANSIBLE_ARGS to load vault credentials}"
KEY="${TNRANGE_APPROVAL_KEY:-/etc/truenorth/approval.key}"
[ -r "$KEY" ] || { echo "CI approvals need a readable signing key at $KEY"; exit 1; }
export TNRANGE_CI_APPROVER=1

step() { echo; echo "=== $* ($(date -u +%H:%M:%S)) ==="; }
cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "${BUILT:-0}" = 1 ]; then
    step "FAILURE: tearing down $ID"
    $R teardown "$ID" --confirm "$ID" || echo "TEARDOWN FAILED - manual cleanup required"
  fi
  $R revoke "$ID" >/dev/null || true
  echo "canary exit $rc, log $LOG"
  exit "$rc"
}
trap cleanup EXIT

step "unit tests and lint"
python3 -m unittest discover -s tests
ansible-lint playbooks/

step "blackout"
$R blackout-check

step "discover live VLANs"
$R discover

step "compile (reproducible)"
$R compile "$ID" --write-allocation
SHA1=$(python3 -c "import json;print(json.load(open('build/$ID/plan.json'))['plan_sha256'])")
$R compile "$ID" >/dev/null
SHA2=$(python3 -c "import json;print(json.load(open('build/$ID/plan.json'))['plan_sha256'])")
[ "$SHA1" = "$SHA2" ] || { echo "plan is not reproducible: $SHA1 vs $SHA2"; exit 1; }

step "check mode"
$R check "$ID"

step "approve (CI identity, 3 hours)"
$R approve "$ID" --action build --action reset --action teardown --hours 3 --by "ci-canary"

step "build"
BUILT=1
$R build "$ID"
$R manifest "$ID"
$R revoke "$ID" >/dev/null
$R approve "$ID" --action build --action reset --action teardown --hours 3 --by "ci-canary"

step "idempotency (second build must change nothing)"
ANSIBLE_STDOUT_CALLBACK=default $R build "$ID" | tee "build/$ID/idempotency.log"
grep -E "changed=0 " "build/$ID/idempotency.log" >/dev/null || { echo "second build changed objects"; exit 1; }

step "drift"
$R drift "$ID"

step "reset"
$R reset "$ID"
$R manifest "$ID"

step "teardown"
$R teardown "$ID" --confirm "$ID"
BUILT=0

step "PASS"
