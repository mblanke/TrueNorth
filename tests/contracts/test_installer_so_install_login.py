# Author: Marc-Andre Blanke
# Created: 2026-11-23T21:03:30Z
"""The installer renders Security Onion's install login (VSPHERE_SO_INSTALL_*).

Found on site 2026-11-23: env.production.j2 never wrote VSPHERE_SO_INSTALL_USER /
VSPHERE_SO_INSTALL_PASSWORD, so an installed worker had neither and every SOC range failed
at deploy with "no install login" (worker/provisioners/vsphere_securityonion.py). The user
defaults to soadmin, the name the API, compose and the worker already use; the password to
vault_vsphere_so_install_password, else the generic template build password (ADR 0012 §5).
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
GROUP_VARS = INSTALL / "inventory/group_vars/all/main.yml"
ENV_J2 = INSTALL / "roles/tn_config/templates/env.production.j2"
PREFLIGHT = INSTALL / "roles/tn_preflight/tasks/main.yml"
KEYS = (
    "TN_TEMPLATE_LINUX_USER",
    "TN_TEMPLATE_LINUX_PASSWORD",
    "VSPHERE_SO_INSTALL_USER",
    "VSPHERE_SO_INSTALL_PASSWORD",
)
FAKE_PW = "Fake-Test-Pw-1!"  # a test value, not a real password


def _rendered(**vault) -> dict[str, str]:
    """The template-login lines of env.production.j2, with group_vars' defaults."""
    gv = yaml.safe_load(GROUP_VARS.read_text())
    j = jinja2.Environment(undefined=jinja2.StrictUndefined)
    ctx: dict = {"vault_template_linux_password": vault.pop("vault_template_linux_password", "")}
    if "vault_vsphere_so_install_password" in vault:
        ctx["vault_vsphere_so_install_password"] = vault.pop("vault_vsphere_so_install_password")
    j.undefined = jinja2.Undefined  # group_vars use `| default(...)` on optional vault keys
    ctx["tn_template_linux_user"] = gv["tn_template_linux_user"]
    ctx["tn_template_linux_password"] = j.from_string(gv["tn_template_linux_password"]).render(**ctx)
    for key in ("tn_vsphere_so_install_user", "tn_vsphere_so_install_password"):
        ctx[key] = vault[key] if key in vault else j.from_string(str(gv[key])).render(**ctx)
    j.undefined = jinja2.StrictUndefined
    lines = "\n".join(line for line in ENV_J2.read_text().splitlines() if line.startswith(KEYS))
    return dict(line.split("=", 1) for line in j.from_string(lines).render(**ctx).splitlines())


def test_the_default_user_is_the_name_the_api_and_worker_already_use():
    """The API's staff-login row, its reserved logins and the template-login check all name
    soadmin; rendering another user would silently change them on upgrade."""
    accounts = (ROOT / "control-plane/api/app/range_credentials/accounts.py").read_text()
    api_default = re.search(r'^DEFAULT_SO_USER = "([^"]+)"', accounts, re.M).group(1)
    compose = (ROOT / "infra/platform/docker/compose.prod.yml").read_text()
    assert f"VSPHERE_SO_INSTALL_USER: ${{VSPHERE_SO_INSTALL_USER:-{api_default}}}" in compose
    assert _rendered()["VSPHERE_SO_INSTALL_USER"] == f"'{api_default}'" == "'soadmin'"


def test_the_default_password_is_the_template_linux_build_password():
    env = _rendered(vault_template_linux_password=FAKE_PW)
    assert env["VSPHERE_SO_INSTALL_PASSWORD"] == env["TN_TEMPLATE_LINUX_PASSWORD"] == f"'{FAKE_PW}'"
    # An empty vault_vsphere_so_install_password (as vault.yml.example ships it) falls back too.
    env = _rendered(vault_template_linux_password=FAKE_PW, vault_vsphere_so_install_password="")
    assert env["VSPHERE_SO_INSTALL_PASSWORD"] == f"'{FAKE_PW}'"


def test_a_vault_so_password_or_explicit_login_wins_and_is_single_quoted():
    env = _rendered(vault_template_linux_password=FAKE_PW, vault_vsphere_so_install_password="So-Fake-3#")
    assert env["VSPHERE_SO_INSTALL_PASSWORD"] == "'So-Fake-3#'"
    env = _rendered(
        vault_template_linux_password=FAKE_PW,
        tn_vsphere_so_install_user="sobuild",
        tn_vsphere_so_install_password="Other-Fake-2$",
    )
    assert env["VSPHERE_SO_INSTALL_USER"] == "'sobuild'"
    assert env["VSPHERE_SO_INSTALL_PASSWORD"] == "'Other-Fake-2$'"
    assert env["TN_TEMPLATE_LINUX_PASSWORD"] == f"'{FAKE_PW}'"


def test_unset_vault_renders_empty_not_an_error():
    assert _rendered()["VSPHERE_SO_INSTALL_PASSWORD"] == "''"


def test_the_vault_example_ships_the_optional_key_empty():
    example = yaml.safe_load((INSTALL / "inventory/group_vars/all/vault.yml.example").read_text())
    assert example["vault_vsphere_so_install_password"] == ""


def _warns(**ctx) -> bool:
    """Evaluate the warning task's `when` list the way Ansible does (all must hold)."""
    tasks = yaml.safe_load(PREFLIGHT.read_text())
    [warn] = [t for t in tasks if t["name"] == "Preflight — Security Onion install login is set (vSphere)"]
    j = jinja2.Environment()
    j.filters["bool"] = lambda v: v in (True, "True", "true", "yes", 1)
    return all(j.from_string("{{ (" + cond + ") }}").render(**ctx) == "True" for cond in warn["when"])


def test_preflight_warns_only_on_vsphere_with_a_login_part_missing():
    full = {"tn_vsphere_so_install_user": "soadmin", "tn_vsphere_so_install_password": FAKE_PW}
    assert not _warns(tn_uses_vcenter=True, **full)
    assert _warns(tn_uses_vcenter=True, **{**full, "tn_vsphere_so_install_password": ""})
    assert _warns(tn_uses_vcenter="True", **{**full, "tn_vsphere_so_install_user": ""})
    assert not _warns(tn_uses_vcenter=False, **{**full, "tn_vsphere_so_install_password": ""})


def test_preflight_guards_quotes_and_never_prints_a_value():
    tasks = yaml.safe_load(PREFLIGHT.read_text())
    [quotes] = [t for t in tasks if t["name"] == "Preflight — the template build login is usable"]
    assert "tn_vsphere_so_install_user" in quotes["vars"]["_vals"]
    assert "tn_vsphere_so_install_password" in quotes["vars"]["_vals"]
    [warn] = [t for t in tasks if t["name"] == "Preflight — Security Onion install login is set (vSphere)"]
    assert "{{" not in warn["ansible.builtin.debug"]["msg"]  # names the variables, never renders one


def test_the_env_file_is_still_rendered_with_no_log():
    tasks = yaml.safe_load((INSTALL / "roles/tn_config/tasks/env.yml").read_text())
    [render] = [t for t in tasks if (t.get("ansible.builtin.template") or {}).get("src") == "env.production.j2"]
    assert render["no_log"] is True
