"""The AD path the CI `ad` lane qualifies is the path the installer runs (plan M2).

tests/integration/test_ad_directory.py proves the behaviour against a directory; these
keep the wiring it depends on from drifting without a directory: the lane federates
through the role's own task files, the local recovery administrator comes before the
federation, the fixture stays on loopback and pinned, and staging stays off by default.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
KC_TASKS = INSTALL / "roles/tn_keycloak/tasks"
FIXTURE = ROOT / "tools/ad-test-dc"


def _yaml(path: Path):
    return yaml.safe_load(path.read_text())


def _includes(tasks: list) -> list[str]:
    return [t["ansible.builtin.include_tasks"] for t in tasks if "ansible.builtin.include_tasks" in t]


def test_local_recovery_admin_is_created_before_the_federation():
    order = _includes(_yaml(KC_TASKS / "main.yml"))
    assert order.index("local_identity.yml") < order.index("ldap_federation.yml") < order.index("ldap_sync.yml")
    local = next(t for t in _yaml(KC_TASKS / "main.yml") if t.get("ansible.builtin.include_tasks") == "local_identity.yml")
    assert local["when"] == "not tn_ldap_enabled | bool or tn_keycloak_local_admin | bool"


def test_the_lane_federates_through_the_role_task_files():
    play = _yaml(FIXTURE / "federate.yml")[0]
    used = [t["ansible.builtin.include_role"]["tasks_from"] for t in play["tasks"] if "ansible.builtin.include_role" in t]
    assert used == ["admin_token", "local_identity", "local_admin_retire", "ldap_federation",
                    "identity_scope", "ldap_sync"]
    for name in used:
        assert (KC_TASKS / f"{name}.yml").is_file()


def test_bind_password_has_one_name_and_comes_from_the_vault():
    gv = _yaml(INSTALL / "inventory/group_vars/all/main.yml")
    # Empty by default so sites WITHOUT AD need no vault value; with AD, preflight refuses
    # an empty one (below) rather than letting Keycloak try an anonymous bind.
    assert gv["tn_ldap_bind_password"] == "{{ vault_ldap_bind_password | default('') }}"
    pre = _yaml(INSTALL / "roles/tn_preflight/tasks/main.yml")
    guard = next(t for t in pre if t.get("name") == "Preflight — AD has a bind password")
    assert guard["when"] == "tn_ldap_enabled | bool"
    assert guard["ansible.builtin.assert"]["that"] == "tn_ldap_bind_password | default('') | string | length > 0"
    assert gv["tn_keycloak_local_admin"] is False
    body = _yaml(INSTALL / "roles/tn_keycloak/defaults/main.yml")["tn_kc_ldap_body"]["config"]
    assert body["bindCredential"] == ["{{ tn_ldap_bind_password }}"]
    for f in [KC_TASKS / "ldap_federation.yml", INSTALL / "roles/tn_tls/tasks/main.yml"]:
        assert not re.search(r"\{\{[^}]*vault_ldap_bind_password", f.read_text()), f


def test_federation_has_ldap_timeouts():
    body = _yaml(INSTALL / "roles/tn_keycloak/defaults/main.yml")["tn_kc_ldap_body"]["config"]
    assert int(body["connectionTimeout"][0]) > 0 and int(body["readTimeout"][0]) > 0


def test_group_mapper_matches_exactly_the_tn_group_dns():
    mapper = next(m for m in _yaml(INSTALL / "roles/tn_keycloak/defaults/main.yml")["tn_kc_ldap_mappers"]
                  if m["name"] == "ad-groups")
    assert mapper["config"]["groups.ldap.filter"] == ["{{ tn_kc_ldap_groups_filter }}"]
    assert mapper["config"]["mode"] == ["READ_ONLY"]
    flt = _yaml(INSTALL / "roles/tn_keycloak/defaults/main.yml")["tn_kc_ldap_groups_filter"]
    assert "distinguishedName=CN={{ g }},{{ tn_ldap_groups_dn }}" in flt and "tn_ad_groups" in flt


def test_sub_mappers_are_reconciled_and_ad_groups_must_come_from_ad():
    fed = _yaml(KC_TASKS / "ldap_federation.yml")
    put = next(t for t in fed if t.get("name") == "Keycloak — reconcile existing LDAP sub-mappers")
    assert put["ansible.builtin.uri"]["method"] == "PUT"
    sync = (KC_TASKS / "ldap_sync.yml").read_text()
    assert "direction=fedToKeycloak" in sync
    check = next(t for t in _yaml(KC_TASKS / "ldap_sync.yml") if t.get("name") == "Keycloak — every TN-* group was found in AD")
    assert "ansible.builtin.assert" in check  # fails, not debug


def test_turning_the_local_admin_off_disables_it():
    main = _yaml(KC_TASKS / "main.yml")
    inc = next(t for t in main if t.get("ansible.builtin.include_tasks") == "local_admin_retire.yml")
    assert "not tn_keycloak_local_admin | bool" in inc["when"] and "tn_ldap_enabled | bool" in inc["when"]
    retire = _yaml(KC_TASKS / "local_admin_retire.yml")
    put = next(t for t in retire if "disable" in t["name"])
    assert put["ansible.builtin.uri"]["body"] == {"enabled": False}
    assert "rejectattr('federationLink', 'defined')" in put["loop"]  # never an AD account


def test_import_check_does_not_list_users():
    """GET /users imports directory users on the fly; one it cannot import fails the list."""
    text = (KC_TASKS / "ldap_sync.yml").read_text()
    assert "/users?" not in text
    assert "tn_kc_sync.json.added" in text and "tn_kc_sync.json.failed" in text


def test_staging_keeps_ad_off_and_the_local_admin_on():
    host = _yaml(INSTALL / "inventory/staging.yml")["all"]["children"]["platform"]["hosts"]["tn-staging"]
    assert host["tn_ldap_enabled"] is False
    assert host["tn_keycloak_local_admin"] is True
    # The owner's Windows DC, ready to uncomment (coordinator flips tn_ldap_enabled).
    text = (INSTALL / "inventory/staging.yml").read_text()
    for line in ("# tn_dc_host: tn-dc01.corp.tnrange.lab", "# tn_dc_ip: 192.168.1.224",
                 '# tn_dns_servers: ["192.168.1.224"]'):
        assert line in text, line
    assert not host["tn_bootstrap_admin_upn"].lower().endswith("@" + _yaml(
        INSTALL / "inventory/group_vars/all/main.yml")["tn_domain"].lower())
    # No test DC on staging: nothing in the installer deploys the Samba fixture.
    assert "ad-test-dc" not in "".join(p.read_text() for p in (INSTALL / "playbooks").glob("*.yml"))


def test_fixture_is_pinned_and_loopback_only():
    assert re.search(r"^FROM debian:bookworm-slim@sha256:[0-9a-f]{64}$", (FIXTURE / "Dockerfile").read_text(), re.M)
    compose = _yaml(FIXTURE / "compose.yml")
    prod = _yaml(ROOT / "infra/platform/docker/compose.prod.yml")
    assert compose["services"]["keycloak"]["image"] == prod["services"]["keycloak"]["image"]
    for name, svc in compose["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)
    # Only the DC's pair and the CA's public certificate reach the DC; no CA key anywhere.
    mounts = "\n".join(str(v) for s in compose["services"].values() for v in s.get("volumes", []))
    assert "ca-private" not in mounts and "ca.key" not in mounts


def test_ci_has_the_ad_lane_and_requires_a_pass():
    job = _yaml(ROOT / ".github/workflows/ci.yml")["jobs"]["ad"]
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "tools/ad-test-dc/run.sh up" in runs and "tools/ad-test-dc/run.sh federate" in runs
    assert "junit_require_pass.py build/ad-test-dc/junit.xml" in runs
    paths = next(s["with"]["path"] for s in job["steps"] if "upload-artifact" in s.get("uses", ""))
    assert "secrets" not in paths and "ca-private" not in paths
