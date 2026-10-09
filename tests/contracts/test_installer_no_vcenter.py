"""A site with no vCenter (mock provisioner) installs without touching vSphere.

tn_uses_vcenter derives from tn_provisioner_backend. When it is false, preflight skips
the vCenter reachability check and lets vault_vsphere_password stay a placeholder,
90-vsphere does nothing, and the env file renders no vCenter password.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"


def _yaml(rel: str):
    return yaml.safe_load((INSTALL / rel).read_text())


def _preflight_task(name_start: str) -> dict:
    tasks = _yaml("roles/tn_preflight/tasks/main.yml")
    return next(t for t in tasks if t.get("name", "").startswith(name_start))


def test_uses_vcenter_follows_the_provisioner_backend() -> None:
    defaults = _yaml("inventory/group_vars/all/main.yml")
    assert defaults["tn_provisioner_backend"] == "vsphere_api"
    assert defaults["tn_uses_vcenter"] == "{{ 'vsphere' in tn_provisioner_backend }}"


def test_preflight_skips_vcenter_reachability_without_vcenter() -> None:
    task = _preflight_task("Preflight — vCenter API reachable")
    assert task["when"] == "tn_uses_vcenter | bool"


def test_vsphere_password_placeholder_is_allowed_only_without_vcenter() -> None:
    task = _preflight_task("Preflight — no vault value is still a CHANGE_ME placeholder")
    skip = task["vars"]["_skip"]
    assert "vault_vsphere_password" in skip
    assert "tn_uses_vcenter" in skip


def test_vsphere_playbook_is_a_no_op_without_vcenter() -> None:
    play = _yaml("playbooks/90-vsphere.yml")[0]
    role = next(r for r in play["roles"] if r["role"] == "tn_vsphere")
    assert role["when"] == "tn_uses_vcenter | bool"


def test_env_file_renders_no_vcenter_password_without_vcenter() -> None:
    env = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    assert "VSPHERE_PASSWORD={{ vault_vsphere_password if tn_uses_vcenter | bool else '' }}" in env


def test_staging_has_no_vcenter() -> None:
    inv = _yaml("inventory/staging.yml")
    host = inv["all"]["children"]["platform"]["hosts"]["tn-staging"]
    assert host["tn_provisioner_backend"] == "mock"
