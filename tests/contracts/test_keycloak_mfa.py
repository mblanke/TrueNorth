"""MFA in Keycloak: what the installer configures, held against the API's rule (v1.2.0).

The behaviour itself is proven on the pinned Keycloak image by scripts/check_keycloak_mfa.sh
(CI job keycloak-mfa). These keep its inputs in step:

* the privileged groups are derived from tn_registration_group_role_map and tn_mfa_roles,
  and the spec's default list (used by CI) equals that derivation;
* the AMR mapper is on the truenorth-identity scope, the same one CI adds;
* the admin token reaches keycloak_mfa.py on stdin, with no_log;
* the realm file holds no authentication flows (they did not import on Keycloak 24);
* the installer's roles match the API's defaults, and production requires all of them.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "install/roles/tn_keycloak"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
GROUP_VARS = yaml.safe_load((ROOT / "install/inventory/group_vars/all/main.yml").read_text())
SPEC = json.loads((ROLE / "files/mfa-spec.json").read_text())
REALM = json.loads((ROOT / "infra/keycloak/realm-truenorth.json").read_text())
MFA_TASKS = yaml.safe_load((ROLE / "tasks/mfa.yml").read_text())


def _derived_groups() -> list[str]:
    roles = set(GROUP_VARS["tn_mfa_roles"])
    return [g for g, r in GROUP_VARS["tn_registration_group_role_map"].items() if r in roles]


def test_mfa_roles_are_the_four_privileged_roles():
    assert set(GROUP_VARS["tn_mfa_roles"]) == {"admin", "instructor", "range_ops", "observer"}
    from app import mfa_policy

    assert set(GROUP_VARS["tn_mfa_roles"]) == mfa_policy.PRIVILEGED_ROLES
    assert set(mfa_policy.DEFAULT_MFA_ROLES.split(",")) == mfa_policy.PRIVILEGED_ROLES


def test_spec_groups_equal_the_derived_privileged_groups():
    assert sorted(SPEC["groups"]) == sorted(_derived_groups())
    assert "TN-Content-Authors" in SPEC["groups"]  # maps to instructor
    assert "TN-Students" not in SPEC["groups"]


def test_installer_derives_the_groups_instead_of_listing_them():
    expr = DEFAULTS["tn_kc_mfa_groups"]
    assert "tn_registration_group_role_map" in expr and "tn_mfa_roles" in expr
    task = next(t for t in MFA_TASKS if "ansible.builtin.command" in t)
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[argv.index("--groups") + 1] == "{{ tn_kc_mfa_groups | join(',') }}"


def test_admin_token_goes_on_stdin_with_no_log():
    task = next(t for t in MFA_TASKS if "ansible.builtin.command" in t)
    assert task["no_log"] is True
    assert "access_token" in task["ansible.builtin.command"]["stdin"]
    assert not any("token" in str(a) for a in task["ansible.builtin.command"]["argv"])


def test_the_mfa_tasks_run_last_in_the_role():
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    assert tasks[-1]["ansible.builtin.include_tasks"] == "mfa.yml"


def test_amr_mapper_is_on_the_identity_scope_as_in_ci():
    mappers = {m["protocolMapper"]: m for m in DEFAULTS["tn_kc_protocol_mappers"]}
    assert mappers["oidc-amr-mapper"]["config"]["access.token.claim"] == "true"
    src = (ROOT / "scripts/check_keycloak_mfa.py").read_text()
    tree = ast.parse(src)
    amr = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == "AMR_MAPPER" for t in n.targets))
    ci = ast.literal_eval(amr.value)
    assert ci["protocolMapper"] == "oidc-amr-mapper" and ci["config"] == mappers["oidc-amr-mapper"]["config"]


def test_realm_file_has_no_authentication_flows():
    assert "authenticationFlows" not in REALM
    assert "browserFlow" not in REALM


def test_spec_flow_is_otp_only_and_level_gated():
    """WebAuthn/OTP as an ALTERNATIVE pair ended on an error page on Keycloak 24 (no
    enrolment); the level-1 subflow keeps level 2 from running on every sign-in."""
    text = json.dumps(SPEC["flow"])
    assert "webauthn" not in text
    forms = next(n for n in SPEC["flow"] if n.get("flow") == "tn-browser-forms")
    loa1, loa2 = forms["executions"]
    assert loa1["executions"][0]["config"]["loa-condition-level"] == "1"
    assert loa2["executions"][0]["config"]["loa-condition-level"] == "2"
    assert SPEC["realm_attributes"]["acr.loa.map"] == {"pwd": 1, "mfa": 2}
    by_role = next(n for n in loa1["executions"] if n.get("flow") == "tn-mfa-by-role")
    conds = [e["config"] for e in by_role["executions"] if e["provider"] == "conditional-user-role"]
    assert {"condUserRole": "tn-mfa-required", "negate": "false"} in conds
    assert {"condUserRole": "tn-mfa-exempt", "negate": "true"} in conds


def test_amr_reference_values_match_the_api():
    from app import mfa_policy

    refs = set(re.findall(r'"default.reference.value": "(\w+)"', json.dumps(SPEC)))
    assert refs == {"pwd", "otp"}
    assert "otp" in mfa_policy.amr_values() and "pwd" not in mfa_policy.amr_values()
    acr_names = {k for k, v in SPEC["realm_attributes"]["acr.loa.map"].items() if v >= 2}
    assert acr_names == set(mfa_policy.acr_values())


def test_demo_exemption_only_on_a_test_host_with_the_demo_loaded():
    expr = GROUP_VARS["tn_mfa_demo_exempt_effective"]
    for needed in ("tn_mfa_demo_exempt", "tn_load_shipped_demo", "tn_arc2_enabled", "tn_arc2_auto_accept"):
        assert needed in expr
    assert GROUP_VARS["tn_mfa_demo_exempt"] is False
    assert "tn_mfa_demo_exempt_effective" in DEFAULTS["tn_kc_mfa_exempt_users"]
    env = (ROOT / "install/roles/tn_config/templates/env.production.j2").read_text()
    block = env[env.index("{% if tn_mfa_demo_exempt_effective"):]
    assert "TN_MFA_DEMO_EXEMPT=true" in block[: block.index("{% endif %}")]


def test_env_and_compose_pass_the_mfa_roles_to_the_api():
    env = (ROOT / "install/roles/tn_config/templates/env.production.j2").read_text()
    assert "TN_MFA_ROLES={{ tn_mfa_roles | join(',') }}" in env
    compose = yaml.safe_load((ROOT / "infra/platform/docker/compose.prod.yml").read_text())
    api = compose["services"]["api"]["environment"]
    assert api["TN_MFA_ROLES"].startswith("${TN_MFA_ROLES:-admin,instructor,range_ops,observer}")
    assert api["TN_MFA_DEMO_EXEMPT"] == "${TN_MFA_DEMO_EXEMPT:-false}"


def test_the_ci_job_runs_the_check():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = ci["jobs"]["keycloak-mfa"]["steps"]
    assert any(s.get("run") == "bash scripts/check_keycloak_mfa.sh" for s in steps)


# ── A Keycloak that drops the connection (PR #165 helm-kind) ──────────────────
def _admin_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("keycloak_mfa_under_test", ROOT / "scripts/keycloak_mfa.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"realm": "truenorth"}'


def test_idempotent_requests_are_retried_on_a_dropped_connection(monkeypatch):
    import http.client

    mod = _admin_module()
    calls = []

    def urlopen(req, timeout):
        calls.append(req.get_method())
        if len(calls) < 3:
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    assert mod.Admin("http://kc/auth", "truenorth", "t").get("") == {"realm": "truenorth"}
    assert calls == ["GET", "GET", "GET"]


def test_a_post_is_never_retried_and_a_dead_keycloak_is_a_keycloak_error(monkeypatch):
    import http.client

    import pytest

    mod = _admin_module()
    calls = []

    def urlopen(req, timeout):
        calls.append(req.get_method())
        raise http.client.RemoteDisconnected("gone")

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    kc = mod.Admin("http://kc/auth", "truenorth", "t")
    with pytest.raises(mod.KeycloakError, match="no answer after 1 attempt"):
        kc.call("POST", "/groups", {"name": "x"})
    assert calls == ["POST"]
    calls.clear()
    with pytest.raises(mod.KeycloakError, match="no answer after 5 attempt"):
        kc.get("")
    assert calls == ["GET"] * 5
