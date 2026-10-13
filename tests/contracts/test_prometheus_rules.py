"""Prometheus alert rules: valid, unit-tested, documented, and fed by something real.

* ``promtool check rules`` on alerts.yml and ``promtool test rules`` on alerts.test.yml
  (the backup and host-probe alerts fire on failure and stay quiet when healthy). promtool
  comes from PATH, else from the prom/prometheus image compose.prod.yml pins, through
  docker; with neither the test skips.
* Every alert in alerts.yml is named in docs/operations.md's alert table and every name
  there exists (the doc once promised a "WebSocket > 8000" alert no rule implemented).
* scripts/monitoring/host-probes.sh writes the series those rules read, for OpenSearch,
  Moodle and the ARC² runner, against a stand-in docker and systemctl.
* node_exporter reads the textfile directory the scripts write to.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/platform/docker"
PROM = DOCKER / "monitoring/prometheus"
OPERATIONS = ROOT / "docs/operations.md"
PROBES = ROOT / "scripts/monitoring/host-probes.sh"


def _alerts() -> list[dict]:
    rules = yaml.safe_load((PROM / "alerts.yml").read_text())
    return [r for g in rules["groups"] for r in g["rules"] if "alert" in r]


def _prometheus_image() -> str:
    compose = yaml.safe_load((DOCKER / "compose.prod.yml").read_text())
    return compose["services"]["prometheus"]["image"]


def _promtool(*args: str) -> subprocess.CompletedProcess:
    if shutil.which("promtool"):
        cmd = ["promtool", *args]
        cwd = PROM
    else:
        if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
            pytest.skip("needs promtool on PATH, or a working docker for the pinned prom/prometheus image")
        cmd = ["docker", "run", "--rm", "-v", f"{PROM}:/rules:ro", "-w", "/rules", "--entrypoint", "promtool",
               _prometheus_image(), *args]
        cwd = None
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)


def test_promtool_checks_the_rules():
    run = _promtool("check", "rules", "alerts.yml")
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"SUCCESS: {len(_alerts())} rules found" in run.stdout + run.stderr


def test_promtool_unit_tests_pass():
    run = _promtool("test", "rules", "alerts.test.yml")
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_host_written_alerts_have_unit_tests():
    tested = set(re.findall(r"alertname: (\w+)", (PROM / "alerts.test.yml").read_text()))
    host_written = {
        r["alert"] for r in _alerts() if re.search(r"truenorth_(backup|probe|opensearch)_|node_textfile", r["expr"])
    }
    assert host_written == {
        "HostProbesStale", "OpenSearchUnreachable", "OpenSearchClusterRed", "OpenSearchClusterYellow",
        "MoodleDown", "Arc2RunnerDown", "BackupFailed", "BackupIncomplete", "BackupStale",
        "AuditArchiveTampered",
    }
    assert host_written <= tested


def test_every_alert_is_documented_and_every_documented_alert_exists():
    doc = OPERATIONS.read_text()
    section = doc.split("### Alert Rules", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| `(\w+)` \|", section, re.M))
    assert documented == {r["alert"] for r in _alerts()}
    # Nothing the stack does not export or alert on.
    assert "WebSocket Connections > 8000" not in doc
    assert "truenorth_ws_connections" not in doc


def test_node_exporter_reads_the_textfile_directory():
    compose = yaml.safe_load((DOCKER / "compose.prod.yml").read_text())
    ne = compose["services"]["node-exporter"]
    assert "--collector.textfile.directory=/textfile" in ne["command"]
    assert "${TN_DATA_ROOT:-/srv/truenorth}/metrics:/textfile:ro" in ne["volumes"]
    base = (ROOT / "install/roles/tn_base/tasks/main.yml").read_text()
    assert '{ path: "{{ tn_data_root }}/metrics" }' in base
    config = (ROOT / "install/roles/tn_config/tasks/main.yml").read_text()
    assert "METRICS_DIR={{ tn_data_root }}/metrics" in config
    assert "scripts/monitoring/host-probes.sh" in config


# ── host-probes.sh against stand-ins ─────────────────────────────────────
FAKE_DOCKER = r"""#!/usr/bin/env bash
all="$*"
case "$all" in
    *"exec -T opensearch curl"*)
        cat >/dev/null
        case "${FAKE_OS:-green}" in
            down) echo "curl: (7) Failed to connect" >&2; exit 7 ;;
            *) printf '{"cluster_name":"tn","status":"%s","number_of_nodes":1}' "${FAKE_OS:-green}" ;;
        esac ;;
    *" ps -q moodle"*) [ "${FAKE_MOODLE:-healthy}" = gone ] || echo cid-moodle ;;
    "inspect --format"*) case "${FAKE_MOODLE:-healthy}" in
            healthy) echo "running healthy" ;;
            unhealthy) echo "running unhealthy" ;;
            starting) echo "running starting" ;;
            *) echo "exited none" ;;
        esac ;;
    *) echo "fake docker: unhandled: $all" >&2; exit 99 ;;
esac
"""

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
[ "$1" = is-active ] || exit 99
[ "${FAKE_RUNNER:-active}" = active ]
"""


@pytest.fixture
def host(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    (tmp_path / "compose.yml").write_text("services: {}\n")
    (tmp_path / "moodle.yml").write_text("services: {}\n")
    (tmp_path / "env").write_text("OPENSEARCH_SNAPSHOT_AUTH=admin:x\n")
    (tmp_path / "moodle.env").write_text("MOODLE_NODE=default\n")
    (tmp_path / "metrics").mkdir()
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "COMPOSE_FILE": str(tmp_path / "compose.yml"),
        "ENV_FILE": str(tmp_path / "env"),
        "METRICS_DIR": str(tmp_path / "metrics"),
        "MOODLE_COMPOSE_FILE": str(tmp_path / "moodle.yml"),
        "MOODLE_ENV_FILE": str(tmp_path / "moodle.env"),
        "ARC2_RUNNER_UNIT": "truenorth-arc2-runner",
    }


def _probe(env: dict) -> dict[str, float]:
    run = subprocess.run(["bash", str(PROBES)], env=env, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    prom = Path(env["METRICS_DIR"]) / "truenorth_probes.prom"
    assert oct(prom.stat().st_mode & 0o777) == "0o644"
    assert not list(Path(env["METRICS_DIR"]).glob(".truenorth_probes.*")), "temp file left behind"
    series = {}
    for line in prom.read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            series[name] = float(value)
    return series


pytestmark_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytestmark_bash
def test_probes_all_healthy(host):
    s = _probe(host)
    assert s['truenorth_probe_up{component="opensearch"}'] == 1
    assert s['truenorth_probe_up{component="moodle"}'] == 1
    assert s['truenorth_probe_up{component="arc2-runner"}'] == 1
    assert s['truenorth_opensearch_cluster_status{color="green"}'] == 1
    assert s['truenorth_opensearch_cluster_status{color="red"}'] == 0


@pytestmark_bash
@pytest.mark.parametrize("color", ["yellow", "red"])
def test_probes_report_the_cluster_colour(host, color):
    s = _probe({**host, "FAKE_OS": color})
    assert s[f'truenorth_opensearch_cluster_status{{color="{color}"}}'] == 1
    assert s['truenorth_opensearch_cluster_status{color="green"}'] == 0
    assert s['truenorth_probe_up{component="opensearch"}'] == 1


@pytestmark_bash
def test_probes_report_each_component_down(host):
    s = _probe({**host, "FAKE_OS": "down", "FAKE_MOODLE": "unhealthy", "FAKE_RUNNER": "failed"})
    assert s['truenorth_probe_up{component="opensearch"}'] == 0
    assert s['truenorth_probe_up{component="moodle"}'] == 0
    assert s['truenorth_probe_up{component="arc2-runner"}'] == 0
    assert not any(k.startswith("truenorth_opensearch_cluster_status") for k in s), "no colour when unreachable"
    assert _probe({**host, "FAKE_MOODLE": "gone"})['truenorth_probe_up{component="moodle"}'] == 0
    # A first install's healthcheck is "starting" for up to 20 minutes: not down.
    assert _probe({**host, "FAKE_MOODLE": "starting"})['truenorth_probe_up{component="moodle"}'] == 1


@pytestmark_bash
def test_components_not_installed_write_no_series(host):
    env = {k: v for k, v in host.items() if k not in {"MOODLE_COMPOSE_FILE", "MOODLE_ENV_FILE", "ARC2_RUNNER_UNIT"}}
    s = _probe(env)
    assert set(k for k in s if k.startswith("truenorth_probe_up")) == {'truenorth_probe_up{component="opensearch"}'}
