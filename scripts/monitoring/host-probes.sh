#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — host-side probes for Prometheus (node_exporter textfile collector)
# =============================================================================
# Some of what docs/operations.md alerts on has no exporter in the stack: OpenSearch's
# cluster health, the installer's Moodle node, and the ARC² runner (a systemd unit on the
# host, not a container). This script checks them from the host, once a minute (cron,
# installed by install/roles/tn_config), and writes the result for node_exporter's
# textfile collector, which compose.prod.yml points at $TN_DATA_ROOT/metrics. The rules
# are in infra/platform/docker/monitoring/prometheus/alerts.yml.
#
#   truenorth_probe_up{component="opensearch"|"moodle"|"arc2-runner"}   1 ok, 0 down
#   truenorth_opensearch_cluster_status{color="green"|"yellow"|"red"}  1 for the current
#
# A component that is not configured here writes no series, so its alert cannot fire.
# HostProbesStale fires when this file stops being refreshed.
#
# Environment (the cron line sources backup.env, as the backup does; lib.sh):
#   METRICS_DIR        where to write truenorth_probes.prom (required)
#   COMPOSE_FILE, ENV_FILE, OPENSEARCH_URL, OPENSEARCH_SNAPSHOT_CACERT
#                      how to reach OpenSearch (os_curl, inside its container)
#   MOODLE_COMPOSE_FILE, MOODLE_ENV_FILE   the Moodle node, when tn_moodle_enabled
#   ARC2_RUNNER_UNIT   the runner's systemd unit, when tn_arc2_enabled
#   PROBE_SKIP_OPENSEARCH=1  leave OpenSearch out
# =============================================================================
set -uo pipefail
IFS=$'\n\t'

# shellcheck source=scripts/backup/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../backup/lib.sh"
TN_LOG_TAG="truenorth-probes"
# A probe answers in seconds or it is down; os_curl's default is sized for snapshots.
OS_CURL_MAX_TIME="${OS_CURL_MAX_TIME:-20}"

METRICS_DIR="${METRICS_DIR:-}"
[[ -n "${METRICS_DIR}" ]] || die "METRICS_DIR is not set (backup.env)"
[[ -d "${METRICS_DIR}" ]] || die "METRICS_DIR ${METRICS_DIR} does not exist"

UP=()
OS_COLOR=""

# ── OpenSearch: reachable, and the cluster's colour ─────────────────────────
if [[ "${PROBE_SKIP_OPENSEARCH:-0}" != 1 ]] && ( compose_init ) >/dev/null 2>&1; then
    compose_init
    health="$(os_curl "${OPENSEARCH_URL}/_cluster/health" 2>/dev/null)" || health=""
    OS_COLOR="$(printf '%s' "${health}" | grep -o '"status" *: *"[a-z]*"' | head -n 1 | sed -E 's/.*"([a-z]+)"$/\1/')"
    case "${OS_COLOR}" in
        green|yellow|red) UP+=("opensearch 1") ;;
        *) OS_COLOR=""; UP+=("opensearch 0") ;;
    esac
fi

# ── Moodle (tn_moodle_enabled): its container runs and is not unhealthy ──────
# "starting" counts as up: a first install takes up to the healthcheck's start_period.
if [[ "$(moodle_state)" == on ]]; then
    cid="$(moodle_dc ps -q moodle 2>/dev/null | head -n 1)"
    state=""
    if [[ -n "${cid}" ]]; then
        state="$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}" 2>/dev/null)"
    fi
    case "${state}" in
        "running healthy"|"running starting"|"running none") UP+=("moodle 1") ;;
        *) UP+=("moodle 0") ;;
    esac
fi

# ── ARC² runner (tn_arc2_enabled): the systemd unit is active ────────────────
if [[ -n "${ARC2_RUNNER_UNIT:-}" ]]; then
    if systemctl is-active --quiet "${ARC2_RUNNER_UNIT}"; then
        UP+=("arc2-runner 1")
    else
        UP+=("arc2-runner 0")
    fi
fi

# ── Write atomically: node_exporter must never read half a file ─────────────
out="${METRICS_DIR}/truenorth_probes.prom"
tmp="$(mktemp "${METRICS_DIR}/.truenorth_probes.XXXXXX")"
{
    echo "# HELP truenorth_probe_up 1 if the host-side probe of a TrueNorth component passed (scripts/monitoring/host-probes.sh)."
    echo "# TYPE truenorth_probe_up gauge"
    for entry in "${UP[@]+"${UP[@]}"}"; do
        printf 'truenorth_probe_up{component="%s"} %s\n' "${entry% *}" "${entry##* }"
    done
    if [[ -n "${OS_COLOR}" ]]; then
        echo "# HELP truenorth_opensearch_cluster_status 1 for OpenSearch's current cluster health colour, 0 for the others."
        echo "# TYPE truenorth_opensearch_cluster_status gauge"
        for color in green yellow red; do
            printf 'truenorth_opensearch_cluster_status{color="%s"} %d\n' "${color}" "$([[ "${color}" == "${OS_COLOR}" ]] && echo 1 || echo 0)"
        done
    fi
} >"${tmp}"
chmod 0644 "${tmp}"   # node_exporter runs as nobody
mv -f "${tmp}" "${out}"
