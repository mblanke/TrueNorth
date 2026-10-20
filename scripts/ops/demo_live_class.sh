#!/usr/bin/env bash
# Run scripts/ops/demo_live_class.py inside the deployed api container (see its docstring).
#
#   sudo scripts/ops/demo_live_class.sh dry-run|apply|remove-dry-run|remove [extra args]
#
# The manifest of every row the demo made lives on the host at
# ${DEMO_MANIFEST:-/srv/truenorth/state/demo-live-class.json} (root, 600). It goes into the
# container for a removal and comes back out after an apply. Restarts nothing.
set -euo pipefail

MODE="${1:-dry-run}"
shift || true
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${DEMO_MANIFEST:-/srv/truenorth/state/demo-live-class.json}"
API="${DEMO_API_CONTAINER:-$(docker ps --filter label=com.docker.compose.service=api --format '{{.Names}}' | head -1)}"
IN=/tmp/demo_live_class.py
IN_MANIFEST=/tmp/demo-live-class.json

[ "$(id -u)" = 0 ] || { echo "run as root (sudo): the manifest is root-only" >&2; exit 2; }
[ -n "${API}" ] || { echo "no running api container" >&2; exit 2; }

case "${MODE}" in
  dry-run) ARGS=(--dry-run) ;;
  apply) ARGS=(--apply) ;;
  remove-dry-run) ARGS=(--remove --dry-run) ;;
  remove) ARGS=(--remove --apply) ;;
  *) echo "usage: $0 dry-run|apply|remove-dry-run|remove [extra args]" >&2; exit 2 ;;
esac

# Piped, not docker cp: the container runs as a non-root user, and a copied file keeps host owners.
put() { docker exec -i "${API}" sh -c "umask 077; cat > $1"; }
put "${IN}" <"${HERE}/demo_live_class.py"
docker exec "${API}" rm -f "${IN_MANIFEST}"
if [ "${MODE#remove}" != "${MODE}" ]; then
  [ -f "${MANIFEST}" ] || { echo "no manifest at ${MANIFEST}: nothing to remove" >&2; exit 2; }
  put "${IN_MANIFEST}" <"${MANIFEST}"
fi

rc=0
docker exec "${API}" python "${IN}" --manifest "${IN_MANIFEST}" "${ARGS[@]}" "$@" || rc=$?

if [ "${rc}" = 0 ] && [ "${MODE}" = apply ]; then
  [ -d "$(dirname "${MANIFEST}")" ] || { echo "no $(dirname "${MANIFEST}")" >&2; exit 2; }
  umask 077
  docker exec "${API}" cat "${IN_MANIFEST}" >"${MANIFEST}.tmp"
  mv "${MANIFEST}.tmp" "${MANIFEST}"
  chown root:root "${MANIFEST}"
  chmod 600 "${MANIFEST}"
  echo "manifest: ${MANIFEST}"
elif [ "${rc}" = 0 ] && [ "${MODE}" = remove ]; then
  mv "${MANIFEST}" "${MANIFEST}.removed-$(date -u +%Y%m%dT%H%M%SZ)"
  echo "manifest retired: ${MANIFEST}.removed-*"
fi
docker exec "${API}" rm -f "${IN}" "${IN_MANIFEST}"
exit "${rc}"
