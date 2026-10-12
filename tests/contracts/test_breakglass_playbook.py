"""The MFA break-glass playbook resets one user's second factor, and nothing else.

playbooks/breakglass-reset-mfa.yml -> roles/tn_keycloak/tasks/breakglass.yml. The same
REST sequence is exercised against a real Keycloak by scripts/check_keycloak_mfa.sh (iv):
after it, the next sign-in is sent to enrolment again. Here, statically:

* both tn_breakglass_user and tn_breakglass_reason are required;
* the Admin API is reached on the loopback URL only, and token tasks are no_log;
* no task PUTs the realm or touches a flow (MFA is never switched off);
* the user's sessions are logged out, CONFIGURE_TOTP is required;
* an audit row is written in TrueNorth (scripts/audit_note_inprocess.py, mfa_reset).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "install/roles/tn_keycloak"
TASKS = yaml.safe_load((ROLE / "tasks/breakglass.yml").read_text())
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())


def _uri_tasks():
    return [t for t in TASKS if "ansible.builtin.uri" in t]


def test_playbook_includes_the_role_tasks():
    [play] = yaml.safe_load((ROOT / "install/playbooks/breakglass-reset-mfa.yml").read_text())
    [task] = play["tasks"]
    assert task["ansible.builtin.include_role"] == {"name": "tn_keycloak", "tasks_from": "breakglass"}


def test_both_variables_are_required_first():
    first = TASKS[0]["ansible.builtin.assert"]["that"]
    assert any("tn_breakglass_user" in c for c in first)
    assert any("tn_breakglass_reason" in c for c in first)


def test_admin_api_on_loopback_only():
    assert DEFAULTS["tn_kc_internal"].startswith("http://127.0.0.1:")
    for t in _uri_tasks():
        url = t["ansible.builtin.uri"]["url"]
        assert url.startswith("{{ tn_kc_realm_url }}"), url
    token = yaml.safe_load((ROLE / "tasks/admin_token.yml").read_text())
    assert token[0]["ansible.builtin.uri"]["url"].startswith("{{ tn_kc_internal }}")
    assert all(t.get("no_log") is True for t in token)


def test_token_bearing_tasks_are_no_log():
    for t in _uri_tasks():
        assert t.get("no_log") is True, t["name"]


def test_never_changes_the_realm_or_a_flow():
    for t in _uri_tasks():
        uri = t["ansible.builtin.uri"]
        assert "authentication" not in uri["url"], t["name"]
        if uri.get("method") == "PUT":
            assert "/users/" in uri["url"], t["name"]
            assert set(uri["body"]) in ({"requiredActions"}, {"type", "value", "temporary"}), t["name"]
    assert "browserFlow" not in json.dumps(TASKS)


def test_deletes_second_factor_credentials_only():
    delete = next(t for t in _uri_tasks() if t["ansible.builtin.uri"].get("method") == "DELETE")
    assert "/credentials/" in delete["ansible.builtin.uri"]["url"]
    assert "'otp'" in delete["loop"] and "'password'" not in delete["loop"]


def test_requires_enrolment_and_logs_out():
    put = next(t for t in _uri_tasks() if t["ansible.builtin.uri"].get("method") == "PUT"
               and "requiredActions" in t["ansible.builtin.uri"]["body"])
    assert "CONFIGURE_TOTP" in put["vars"]["_actions"]
    logout = next(t for t in _uri_tasks() if t["ansible.builtin.uri"]["url"].endswith("/logout"))
    assert logout["ansible.builtin.uri"]["method"] == "POST"


def test_writes_the_truenorth_audit_note():
    cmd = next(t for t in TASKS if "ansible.builtin.command" in t)["ansible.builtin.command"]["argv"]
    assert "/srcapp/scripts/audit_note_inprocess.py" in cmd
    assert cmd[cmd.index("--action") + 1] == "mfa_reset"
    assert "tn_breakglass_reason" in cmd[cmd.index("--reason") + 1]
    assert "tn_breakglass_operator" in cmd[cmd.index("--operator") + 1]


def test_audit_note_script_writes_a_row(db_session):
    import importlib.util

    spec = importlib.util.spec_from_file_location("audit_note", ROOT / "scripts/audit_note_inprocess.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    row = mod.note(db_session, action="mfa_reset", subject="lost.phone@corp.example", reason="ticket 1",
                   operator="ops1")
    assert row.action == "mfa_reset" and row.resource_id == "lost.phone@corp.example"
    assert json.loads(row.detail) == {"subject": "lost.phone@corp.example", "reason": "ticket 1",
                                      "operator": "ops1", "via": "playbooks/breakglass-reset-mfa.yml"}



# ── Review S1-6: the audit row first; a new password for local accounts ──
def _index(pred) -> int:
    return next(i for i, t in enumerate(TASKS) if pred(t))


def test_the_audit_row_is_written_before_anything_changes():
    audit = _index(lambda t: "ansible.builtin.command" in t)
    first_change = _index(lambda t: t.get("ansible.builtin.uri", {}).get("method") in ("DELETE", "PUT", "POST"))
    assert audit < first_change
    gate = TASKS[audit + 1]["ansible.builtin.assert"]["that"]
    assert "tn_bg_audit.rc == 0" in gate and "tn_breakglass_no_audit" in gate
    warn = TASKS[audit + 2]
    assert "WITHOUT a TrueNorth audit row" in warn["name"] and warn["when"] == "tn_bg_audit.rc != 0"


def test_a_local_account_must_also_set_a_new_password():
    reset = next(t for t in _uri_tasks() if t["ansible.builtin.uri"]["url"].endswith("/reset-password"))
    assert reset["ansible.builtin.uri"]["body"]["temporary"] is True and reset["no_log"] is True
    written = next(t for t in TASKS if "ansible.builtin.copy" in t)
    assert written["ansible.builtin.copy"]["mode"] == "0600" and written["no_log"] is True
    put = next(t for t in _uri_tasks() if t["ansible.builtin.uri"].get("method") == "PUT"
               and "requiredActions" in t["ansible.builtin.uri"]["body"])
    assert "UPDATE_PASSWORD" in put["vars"]["_pw"] and "federationLink" in put["vars"]["_local"]
    assert "tn_breakglass_reset_password" in put["vars"]["_pw"]



def test_the_password_file_is_written_root_only_before_keycloak_changes():
    """Review 2, items 3-4: a failed copy must not leave a password nobody knows."""
    write = _index(lambda t: "ansible.builtin.copy" in t)
    reset = _index(lambda t: t.get("ansible.builtin.uri", {}).get("url", "").endswith("/reset-password"))
    assert write < reset
    task = TASKS[write]
    assert task["become"] is True
    assert (task["ansible.builtin.copy"]["owner"], task["ansible.builtin.copy"]["mode"]) == ("root", "0600")
