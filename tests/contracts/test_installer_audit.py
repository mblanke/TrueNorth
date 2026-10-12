"""The installer gives the audit export its secret, its archive and nothing more (ADR 0011).

* ``audit_signing_seed`` is a generated, persisted (and so escrowed) secret;
* the env file carries it, but compose passes it to worker-scenario only: not the api,
  not beat, not the other workers;
* worker-scenario mounts the archive and consumes the ``default`` queue the tasks run on;
* the archive directory is made for the worker's uid; backup.env names it for backup.sh;
* the install report prints the public key fingerprint (to record out of band).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "infra/platform/docker/compose.prod.yml").read_text())
ENV_J2 = (ROOT / "install/roles/tn_config/templates/env.production.j2").read_text()


def test_the_seed_is_a_generated_persisted_secret():
    secrets = yaml.safe_load((ROOT / "install/roles/tn_config/defaults/main.yml").read_text())["tn_generated_secrets"]
    [seed] = [s for s in secrets if s["name"] == "audit_signing_seed"]
    assert "vault_audit_signing_seed" in seed["value"] and not seed.get("first_start")
    assert "AUDIT_SIGNING_SEED={{ tn_secrets['audit_signing_seed'] }}" in ENV_J2


def _env(service: str) -> dict:
    env = COMPOSE["services"][service].get("environment") or {}
    return env if isinstance(env, dict) else dict(e.split("=", 1) for e in env)


def test_only_worker_scenario_receives_the_seed():
    holders = [name for name in COMPOSE["services"] if "AUDIT_SIGNING_SEED" in _env(name)]
    assert holders == ["worker-scenario"]
    assert _env("worker-scenario")["AUDIT_SIGNING_SEED"] == "${AUDIT_SIGNING_SEED:-}"
    for other in ("api", "beat", "worker-provision", "worker-telemetry", "flower"):
        assert "AUDIT_SIGNING_SEED" not in _env(other), other


def test_worker_scenario_mounts_the_archive_and_runs_the_default_queue():
    svc = COMPOSE["services"]["worker-scenario"]
    assert any(v.endswith(":/var/lib/truenorth/audit-archive") and "AUDIT_ARCHIVE_PATH" in v for v in svc["volumes"])
    assert _env("worker-scenario")["AUDIT_ARCHIVE_DIR"] == "/var/lib/truenorth/audit-archive"
    queues = re.search(r"-Q\s+(\S+)", svc["command"]).group(1).split(",")
    assert "default" in queues
    assert "AUDIT_ARCHIVE_PATH" not in str(COMPOSE["services"]["api"].get("volumes"))


def test_archive_directory_backup_env_and_retention():
    group_vars = yaml.safe_load((ROOT / "install/inventory/group_vars/all/main.yml").read_text())
    assert group_vars["tn_audit_hot_retention_days"] == 90
    base = (ROOT / "install/roles/tn_base/tasks/main.yml").read_text()
    assert '{ path: "{{ tn_audit_archive_path }}", owner: "10001", group: "10001", mode: "0750" }' in base
    config = (ROOT / "install/roles/tn_config/tasks/main.yml").read_text()
    assert "AUDIT_ARCHIVE_PATH={{ tn_audit_archive_path }}" in config  # backup.env
    assert "AUDIT_HOT_RETENTION_DAYS={{ tn_audit_hot_retention_days }}" in ENV_J2


def test_install_report_prints_the_key_fingerprint():
    text = (ROOT / "install/playbooks/99-validate.yml").read_text()
    assert "-m worker.audit_signing" in text and "record it out of band" in text
