"""The installer can load the shipped content/ into an installed host, off by default.

A production install starts with an empty catalogue by design; staging and demo hosts
opt in. The load goes through the API in-process as the bootstrap administrator
(scripts/load_content_inprocess.py), so no token or password is involved.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"


def _task(name: str) -> dict:
    tasks = yaml.safe_load((INSTALL / "roles/tn_seed/tasks/main.yml").read_text())
    return next(t for t in tasks if t.get("name") == name)


def test_off_by_default_and_on_for_staging() -> None:
    defaults = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    for var in ("tn_load_shipped_content", "tn_publish_shipped_content", "tn_load_shipped_demo"):
        assert defaults[var] is False, var
    host = yaml.safe_load((INSTALL / "inventory/staging.yml").read_text())["all"]["children"]["platform"]["hosts"][
        "tn-staging"
    ]
    assert host["tn_load_shipped_content"] is True
    assert host["tn_publish_shipped_content"] is True


def test_the_load_runs_in_process_as_the_bootstrap_admin_without_credentials() -> None:
    task = _task("Seed — load the shipped content")
    assert task["when"] == "tn_load_shipped_content | bool"
    cmd = task["ansible.builtin.command"]["cmd"]
    assert "scripts/load_content_inprocess.py" in cmd and "--admin {{ tn_bootstrap_admin_upn | quote }}" in cmd
    assert "--no-deps" in cmd and ":/srcapp:ro" in cmd
    assert "environment" not in task  # nothing secret is passed to it


def test_the_loader_replaces_only_the_token_check() -> None:
    # The harness is shared with scripts/arc2_batch_inprocess.py (scripts/_inprocess_api.py).
    src = (ROOT / "scripts/load_content_inprocess.py").read_text() + (ROOT / "scripts/_inprocess_api.py").read_text()
    assert "from _inprocess_api import connect" in src
    assert "dependency_overrides[auth.get_token_identity]" in src
    assert "x-csrf-token" in src  # CSRF stays on: the loader echoes the cookie like a browser
    # No credential of any kind: no bearer header, no Keycloak secret, no env lookups.
    assert "Authorization" not in src and "KEYCLOAK" not in src and "os.environ" not in src
