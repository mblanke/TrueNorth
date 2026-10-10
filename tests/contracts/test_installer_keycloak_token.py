"""The demo-account steps take a fresh Keycloak admin token.

Keycloak's admin-cli tokens live about 60 s; the demo accounts (create, set passwords, record
hashes, groups) outlived the token taken at the start of 60-keycloak and the groups step got
401 on staging (v1.2.0-rc2).
"""

from __future__ import annotations

from pathlib import Path

import yaml

TASKS = Path(__file__).resolve().parents[2] / "install/roles/tn_keycloak/tasks"


def _includes(name: str) -> list[str]:
    return [
        t["name"]
        for t in yaml.safe_load((TASKS / name).read_text())
        if t.get("ansible.builtin.include_tasks") == "admin_token.yml"
    ]


def test_the_token_task_is_shared() -> None:
    tasks = yaml.safe_load((TASKS / "admin_token.yml").read_text())
    assert tasks[0]["register"] == "tn_kc_token" and tasks[0]["no_log"] is True
    assert "tn_kc_headers" in tasks[1]["ansible.builtin.set_fact"]


def test_main_and_the_demo_accounts_take_their_own_token() -> None:
    assert _includes("main.yml")
    demo = yaml.safe_load((TASKS / "demo_accounts.yml").read_text())
    names = [t["name"] for t in demo]
    refreshes = [i for i, t in enumerate(demo) if t.get("ansible.builtin.include_tasks") == "admin_token.yml"]
    groups = names.index("Keycloak — the demo accounts' groups")
    assert refreshes and refreshes[0] == 0
    assert any(i == groups - 1 for i in refreshes), "refresh the token right before the groups step"
