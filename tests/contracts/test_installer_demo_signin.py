"""Demo sign-in: with tn_load_shipped_demo (staging, demo hosts), the demo people can sign in.

60-keycloak gives each shipped demo person a local Keycloak account whose password is a
persisted secret (config/secrets/demo_*_password, generated once like
bootstrap_admin_password); 80-seed points each demo roster row at its account
(app.link_demo_accounts). Off by default and on no production inventory; idempotent;
passwords never on a command line and never logged.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
GROUP_VARS = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
CONFIG_DEFAULTS = yaml.safe_load((INSTALL / "roles/tn_config/defaults/main.yml").read_text())
SECRETS_TASKS = yaml.safe_load((INSTALL / "roles/tn_config/tasks/secrets.yml").read_text())
DEMO_TASKS_TEXT = (INSTALL / "roles/tn_keycloak/tasks/demo_accounts.yml").read_text()
DEMO_TASKS = yaml.safe_load(DEMO_TASKS_TEXT)
SEED_TASKS = yaml.safe_load((INSTALL / "roles/tn_seed/tasks/main.yml").read_text())
DEMO_GATE = "tn_load_shipped_demo | bool"


def _named(tasks: list[dict], name: str) -> dict:
    return next(t for t in tasks if t.get("name") == name)


def _loader_demo_people() -> list[tuple[str, str, str]]:
    tree = ast.parse((ROOT / "scripts/load_content.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "DEMO_PEOPLE" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("DEMO_PEOPLE not found in scripts/load_content.py")


# ── Never on in production ───────────────────────────────────────────


def test_off_by_default_and_on_no_production_inventory() -> None:
    assert GROUP_VARS["tn_load_shipped_demo"] is False
    prod = yaml.safe_load((INSTALL / "inventory/hosts.yml").read_text())
    for host, hvars in prod["all"]["children"]["platform"]["hosts"].items():
        assert not (hvars or {}).get("tn_load_shipped_demo", False), host
    staging = yaml.safe_load((INSTALL / "inventory/staging.yml").read_text())
    assert staging["all"]["children"]["platform"]["hosts"]["tn-staging"]["tn_load_shipped_demo"] is True


def test_demo_secrets_exist_only_where_the_demo_is_on() -> None:
    names = {s["name"] for s in CONFIG_DEFAULTS["tn_demo_secrets"]}
    assert names.isdisjoint({s["name"] for s in CONFIG_DEFAULTS["tn_generated_secrets"]})
    expr = CONFIG_DEFAULTS["tn_secret_list"]
    assert "tn_generated_secrets" in expr
    assert "tn_demo_secrets if tn_load_shipped_demo | default(false) | bool else []" in expr


def test_keycloak_accounts_are_gated_and_local_only() -> None:
    main = yaml.safe_load((INSTALL / "roles/tn_keycloak/tasks/main.yml").read_text())
    local = _named(main, "Keycloak — local identity (no AD)")
    assert local["when"] == "not tn_ldap_enabled | bool"
    inc = _named(local["block"], "Keycloak — the shipped demo people's sign-in accounts")
    assert inc["ansible.builtin.include_tasks"] == "demo_accounts.yml"
    assert DEMO_GATE in inc["when"] and "tn_load_shipped_content | bool" in inc["when"]


def test_the_seed_link_is_gated() -> None:
    task = _named(SEED_TASKS, "Seed — link the demo people to their sign-in accounts")
    assert DEMO_GATE in task["when"] and "not tn_ldap_enabled | bool" in task["when"]
    assert "tn_load_shipped_content | bool" in task["when"]


# ── The people match the loader's ────────────────────────────────────


def test_demo_people_match_the_loader_and_their_roles() -> None:
    people = GROUP_VARS["tn_demo_people"]
    loader = _loader_demo_people()
    assert [p["email"] for p in people] == [e for e, _, _ in loader]
    role_of = dict((e, r) for e, _, r in loader)
    for p in people:
        assert p["group"] in GROUP_VARS["tn_ad_groups"], p
        assert GROUP_VARS["tn_registration_group_role_map"][p["group"]] == role_of[p["email"]], p
        assert p["email"].endswith(".demo@truenorth.test")
        assert p["first_name"] and p["last_name"]  # Keycloak 24's user profile requires both
    assert {p["secret"] for p in people} == {s["name"] for s in CONFIG_DEFAULTS["tn_demo_secrets"]}


# ── Secrets: persisted 0600 by the shared helper, reused, never on argv ──


def test_demo_passwords_are_persisted_by_the_shared_helper_0600_via_stdin() -> None:
    persist = _named(SECRETS_TASKS, "Config — persist every secret (generate any the vault leaves empty)")
    assert persist["loop"] == "{{ tn_secret_list }}"
    cmd = persist["ansible.builtin.shell"]["cmd"]
    assert "umask 077" in cmd  # every file it writes is 0600 (config/secrets/ is 0700)
    assert 'if [ -s "$f" ]' in cmd  # an existing, non-empty file is reused, never regenerated
    assert persist["ansible.builtin.shell"]["stdin"] == "{{ item.value | default('') }}"
    assert persist["no_log"] is True
    for name in ("Config — read persisted secrets back", "Config — assemble the effective secret map"):
        assert _named(SECRETS_TASKS, name)["no_log"] is True
    assert _named(SECRETS_TASKS, "Config — read persisted secrets back")["loop"] == "{{ tn_secret_list }}"


def test_every_task_touching_a_password_is_no_log() -> None:
    for task in DEMO_TASKS:
        if "tn_secrets" in yaml.safe_dump(task):
            assert task.get("no_log") is True, task["name"]
    link = _named(SEED_TASKS, "Seed — link the demo people to their sign-in accounts")
    # The master admin password is passed through the environment, not in the command.
    assert "tn_secrets" not in link["ansible.builtin.command"]["cmd"]
    assert "tn_secrets['keycloak_admin_password']" in link["environment"]["KEYCLOAK_ADMIN_PASSWORD"]


def test_no_password_on_a_command_line() -> None:
    # Keycloak is driven through its Admin REST API (request bodies), never kcadm.sh argv.
    for task in DEMO_TASKS:
        assert not {"ansible.builtin.command", "ansible.builtin.shell"} & task.keys(), task["name"]
    assert "--password" not in DEMO_TASKS_TEXT and "kcadm" not in DEMO_TASKS_TEXT
    cmd = _named(SEED_TASKS, "Seed — link the demo people to their sign-in accounts")["ansible.builtin.command"]["cmd"]
    assert "-e KEYCLOAK_ADMIN_PASSWORD " in cmd and "KEYCLOAK_ADMIN_PASSWORD=" not in cmd
    assert "password" not in cmd.replace("KEYCLOAK_ADMIN_PASSWORD", "").lower()


# ── Idempotent ───────────────────────────────────────────────────────


def test_lookup_before_create() -> None:
    names = [t["name"] for t in DEMO_TASKS]
    lookup = _named(DEMO_TASKS, "Keycloak — do the demo accounts exist?")
    create = _named(DEMO_TASKS, "Keycloak — create the demo accounts (local)")
    assert names.index(lookup["name"]) < names.index(create["name"])
    assert "&exact=true" in lookup["ansible.builtin.uri"]["url"]
    assert create["loop"] == "{{ tn_kc_demo_existing.results }}"
    assert create["when"] == "item.json | length == 0"
    assert create["ansible.builtin.uri"]["status_code"] == [201]


def test_passwords_are_reset_only_when_the_persisted_secret_changed() -> None:
    reset = _named(DEMO_TASKS, "Keycloak — set the demo passwords")
    assert "tn_kc_demo_existing.results[i].json | length > 0" in reset["when"]
    assert "tn_kc_demo_pw_prev.results[i].content" in reset["when"] and "hash('sha256')" in reset["when"]
    record = _named(DEMO_TASKS, "Keycloak — record them (keyed hash)")
    assert record["ansible.builtin.copy"]["mode"] == "0600"
    assert "tn_secrets['secrets_key']" in record["ansible.builtin.copy"]["content"]  # keyed, not a bare hash


def test_group_membership_is_added_only_when_missing() -> None:
    put = _named(DEMO_TASKS, "Keycloak — put each demo account in its group")
    assert put["when"].startswith("item.group not in")


def test_the_link_touches_only_demo_rows_and_never_creates_users() -> None:
    src = (ROOT / "control-plane/api/app/link_demo_accounts.py").read_text()
    assert 'DEMO_DOMAIN_SUFFIX = ".demo@truenorth.test"' in src
    assert "User(" not in src and "db.add(" not in src and ".role" not in src
