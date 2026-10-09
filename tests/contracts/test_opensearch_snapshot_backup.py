"""Telemetry is in the backup: an OpenSearch snapshot repository is set up by default.

On an installed host scripts/backup/backup.sh warned "opensearch: not backed up
(OPENSEARCH_SNAPSHOT_REPO unset)": compose.prod.yml gave OpenSearch no path.repo, nothing
registered a repository, and the installer's backup.env named none. These pin the four
pieces that must agree: the compose bind (path.repo), the installer's directory and
backup.env, the registration (telemetry.pipelines.bootstrap, run by 70-telemetry), and
the backup/restore scripts. No Docker needed; scripts/backup/drill.sh runs the real thing.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/platform/docker"
INSTALL = ROOT / "install"
BACKUP = ROOT / "scripts/backup"
REPO_PATH = "/usr/share/opensearch/snapshots"


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts compose's merge tags (!reset, !override) as plain values."""


for _tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(_tag, lambda loader, node: None)


def _prod() -> dict:
    return yaml.load((DOCKER / "compose.prod.yml").read_text(), Loader=_ComposeLoader)  # noqa: S506 - SafeLoader subclass


def _env(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    return dict(item.split("=", 1) for item in env) if isinstance(env, list) else env


def _task(path: str, name: str) -> dict:
    for task in yaml.safe_load((INSTALL / path).read_text()):
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task {name!r} in {path}")


def _render(text: str, **variables) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    env.filters["bool"] = lambda v: str(v).lower() in ("1", "true", "yes", "on")
    return env.from_string(text).render(**variables)


def _group_vars() -> dict:
    return yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())


# ── compose ───────────────────────────────────────────────────────────
def test_prod_opensearch_has_a_path_repo_on_a_bind_under_the_data_root():
    prod = _prod()
    opensearch = prod["services"]["opensearch"]
    assert _env(opensearch)["path.repo"] == REPO_PATH
    assert f"os-snapshots:{REPO_PATH}" in opensearch["volumes"]
    volume = prod["volumes"]["os-snapshots"]
    assert volume["driver_opts"]["o"] == "bind"
    assert volume["driver_opts"]["device"] == "${TN_DATA_ROOT:-/srv/truenorth}/opensearch-snapshots"
    # Not inside the data directory: a snapshot must outlive the volume it backs up.
    assert prod["volumes"]["os-data"]["driver_opts"]["device"] != volume["driver_opts"]["device"]


def test_the_snapshot_mount_keeps_the_hardened_profile():
    opensearch = _prod()["services"]["opensearch"]
    assert opensearch["cap_drop"] == ["ALL"] and "cap_add" not in opensearch
    assert "no-new-privileges:true" in opensearch["security_opt"]
    # Read-write on purpose (snapshots are written there); every other bind stays :ro.
    for mount in opensearch["volumes"]:
        if mount.startswith("${"):
            assert mount.endswith(":ro"), mount


# ── installer ─────────────────────────────────────────────────────────
def test_installer_creates_the_snapshot_directory_for_the_opensearch_uid():
    task = _task("roles/tn_base/tasks/main.yml", "Base — data directory tree")
    dirs = {item["path"]: item for item in task["loop"]}
    snap = dirs["{{ tn_data_root }}/opensearch-snapshots"]
    assert (snap["owner"], snap["group"]) == ("1000", "1000")
    assert snap["mode"] == "0750"


def test_backup_env_names_the_repository_by_default():
    task = _task("roles/tn_config/tasks/main.yml", "Config — render the backup environment")
    defaults = _group_vars()
    assert defaults["tn_opensearch_snapshot_repo"] == "tn_snapshots"
    common = {
        "tn_compose_file": "/srv/truenorth/app/infra/platform/docker/compose.prod.yml",
        "tn_env_file": "/srv/truenorth/config/.env.production",
        "tn_data_root": "/srv/truenorth",
        "tn_config_dir": "/srv/truenorth/config",
        "tn_backup_escrow_pubkey": "",
        "tn_backup_min_free_gb": 20,
        "tn_backup_max_total_gb": 200,
        "tn_opensearch_snapshot_keep": defaults["tn_opensearch_snapshot_keep"],
    }
    on = _render(
        task["ansible.builtin.copy"]["content"],
        **common,
        tn_opensearch_snapshot_repo=defaults["tn_opensearch_snapshot_repo"],
        tn_opensearch_disable_security=False,
    )
    assert re.search(r"^OPENSEARCH_SNAPSHOT_REPO=tn_snapshots$", on, re.M)
    assert re.search(r"^OPENSEARCH_SNAPSHOT_KEEP=14$", on, re.M)
    assert re.search(r"^OPENSEARCH_SNAPSHOT_CACERT=config/certs/ca.pem$", on, re.M)
    # Security on: backup.sh's own https://localhost:9200 default, never overridden.
    assert "OPENSEARCH_URL=" not in on

    lab = _render(
        task["ansible.builtin.copy"]["content"], **common, tn_opensearch_snapshot_repo="tn_snapshots",
        tn_opensearch_disable_security=True,
    )
    assert re.search(r"^OPENSEARCH_URL=http://localhost:9200$", lab, re.M)

    off = _render(
        task["ansible.builtin.copy"]["content"], **common, tn_opensearch_snapshot_repo="",
        tn_opensearch_disable_security=False,
    )
    assert not re.search(r"^OPENSEARCH_SNAPSHOT_REPO=", off, re.M)


def test_env_file_carries_the_snapshot_credentials_with_security_on():
    template = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    m = re.search(r"\{% if not \(tn_opensearch_disable_security \| bool\) %\}(.*?)\{% endif %\}", template, re.S)
    assert m and "OPENSEARCH_SNAPSHOT_AUTH=admin:{{ tn_secrets['opensearch_admin_password'] }}" in m.group(1)


def test_70_telemetry_registers_the_same_repository_at_path_repo():
    task = _task("roles/tn_telemetry/tasks/main.yml", "Telemetry — bootstrap OpenSearch objects")
    cmd = task["ansible.builtin.command"]["cmd"]
    assert "-e OPENSEARCH_SNAPSHOT_REPO={{ tn_opensearch_snapshot_repo | default('') }}" in cmd
    assert f"-e OPENSEARCH_SNAPSHOT_LOCATION={REPO_PATH}" in cmd
    assert task["failed_when"] == "tn_os_bootstrap.rc != 0"


# ── registration (telemetry.pipelines.bootstrap) ──────────────────────
def _bootstrap(monkeypatch, repo: str | None):
    if repo is None:
        monkeypatch.delenv("OPENSEARCH_SNAPSHOT_REPO", raising=False)
    else:
        monkeypatch.setenv("OPENSEARCH_SNAPSHOT_REPO", repo)
    monkeypatch.delenv("OPENSEARCH_SNAPSHOT_LOCATION", raising=False)
    sys.path.insert(0, str(ROOT / "telemetry"))
    try:
        sys.modules.pop("pipelines.bootstrap", None)
        return importlib.import_module("pipelines.bootstrap")
    finally:
        sys.path.pop(0)


class _Resp:
    def __init__(self, status: int, text: str = "{}"):
        self.status_code = status
        self.text = text
        self.is_success = 200 <= status < 300


class _Client:
    def __init__(self, status: int = 200):
        self.status = status
        self.puts: list[tuple[str, dict]] = []

    def put(self, path, json=None, params=None):
        self.puts.append((path, json))
        return _Resp(self.status)


def test_registration_puts_an_fs_repository_at_path_repo(monkeypatch):
    bootstrap = _bootstrap(monkeypatch, "tn_snapshots")
    client = _Client()
    assert bootstrap.register_snapshot_repository(client) is True
    assert client.puts == [
        ("/_snapshot/tn_snapshots", {"type": "fs", "settings": {"location": REPO_PATH, "compress": True}})
    ]
    # PUT is an upsert: a second run registers the same thing again, nothing else.
    bootstrap.register_snapshot_repository(client)
    assert client.puts[0] == client.puts[1]


def test_no_repository_name_registers_nothing(monkeypatch):
    bootstrap = _bootstrap(monkeypatch, None)
    client = _Client()
    assert bootstrap.register_snapshot_repository(client) is False
    assert client.puts == []


def test_a_refused_registration_fails_the_bootstrap(monkeypatch):
    # e.g. a location outside path.repo, or a directory the opensearch uid cannot write.
    bootstrap = _bootstrap(monkeypatch, "tn_snapshots")
    with pytest.raises(RuntimeError, match="tn_snapshots"):
        bootstrap.register_snapshot_repository(_Client(status=500))


def test_bootstrap_runs_the_registration(monkeypatch):
    bootstrap = _bootstrap(monkeypatch, "tn_snapshots")
    calls: list[str] = []
    for step in ("create_ism_policy", "create_ingest_pipelines", "create_index_templates",
                 "create_initial_indices", "apply_replica_settings", "create_dashboards"):
        monkeypatch.setattr(bootstrap, step, lambda client, _s=step: calls.append(_s))
    monkeypatch.setattr(bootstrap, "register_snapshot_repository", lambda client: calls.append("snapshot"))
    bootstrap.bootstrap("http://opensearch.invalid:9200")
    assert "snapshot" in calls and calls.index("snapshot") < calls.index("create_dashboards")


# ── backup / restore scripts ──────────────────────────────────────────
def test_backup_snapshots_through_the_verified_helper_and_checks_the_result():
    backup = (BACKUP / "backup.sh").read_text()
    lib = (BACKUP / "lib.sh").read_text()
    code = "\n".join(line for line in lib.splitlines() if not line.lstrip().startswith("#"))
    assert "os_curl()" in code and "--cacert" in code and "-K -" in code
    assert not re.search(r"curl\b[^\n]*\s(-k|--insecure)\b", code)
    assert "os_curl -X PUT" in backup and '"state":"SUCCESS"' in backup
    assert "OPENSEARCH_SNAPSHOT_KEEP" in backup and "_cat/snapshots" in backup


def test_restore_restores_the_snapshot_and_checks_it_before_stopping_anything():
    restore = (BACKUP / "restore.sh").read_text()
    assert "--skip-opensearch" in restore
    check = restore.index('CURRENT_STEP="opensearch:check"')
    quiesce = restore.index('CURRENT_STEP="quiesce"')
    restore_call = restore.index("/_restore?wait_for_completion=true")
    assert check < quiesce < restore_call
    # Telemetry only: the security index and other system indices are never replaced.
    assert '"indices":"*,-.*","include_global_state":false' in restore
