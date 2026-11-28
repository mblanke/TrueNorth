# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/audit_note_inprocess.py: one audit_logs row for a host-side action, written
against the real session fixture — an existing subject account supplies tenant and
resource id, an unknown subject is still recorded by name with no tenant, the detail JSON
carries sorted keys and the playbook via, and main() validates reason/operator and prints
the row id. main()'s SessionLocal is pointed at the test session, so nothing outside the
in-memory SQLite is touched. --action is limited to mfa_reset by argparse."""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from app.models import AuditLog, User, UserRole

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("cov_f_audit_note", ROOT / "scripts" / "audit_note_inprocess.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class _Shared:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


@pytest.fixture
def session_local(db_session, monkeypatch):
    import app.db

    monkeypatch.setattr(app.db, "SessionLocal", lambda: _Shared(db_session))
    return db_session


def _user(db, email):
    user = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4()}",
        email=email,
        display_name=email.split("@")[0],
        role=UserRole.student,
        tenant_id=db.query(User).first().tenant_id,
    )
    db.add(user)
    db.commit()
    return user


def test_note_records_an_existing_subject_with_its_tenant_and_resource_id(session_local):
    user = _user(session_local, "subject@example.test")
    row = mod.note(session_local, action="mfa_reset", subject=user.email, reason="lost device", operator="ops")
    assert row.user_id is None  # the actor is the host operator, named in detail
    assert row.tenant_id == user.tenant_id
    assert row.action == "mfa_reset"
    assert row.resource_type == "user"
    assert row.resource_id == str(user.id)
    assert json.loads(row.detail) == {
        "operator": "ops",
        "reason": "lost device",
        "subject": user.email,
        "via": "playbooks/breakglass-reset-mfa.yml",
    }
    assert list(json.loads(row.detail)) == sorted(json.loads(row.detail))  # sort_keys=True


def test_note_records_an_identity_with_no_account_by_name(session_local):
    row = mod.note(
        session_local,
        action="mfa_reset",
        subject="stranger@example.test",
        reason="offboarded without an account",
        operator="ops",
    )
    assert row.user_id is None and row.tenant_id is None
    assert row.resource_id == "stranger@example.test"
    assert json.loads(row.detail)["subject"] == "stranger@example.test"


def test_main_writes_the_row_and_prints_its_id(session_local, capsys):
    user = _user(session_local, "reset@example.test")
    rc = mod.main(
        [
            "--action",
            "mfa_reset",
            "--subject",
            user.email,
            "--reason",
            "  MFA reset after phishing  ",
            "--operator",
            "  ops  ",
        ]
    )
    assert rc == 0
    row = session_local.query(AuditLog).order_by(AuditLog.timestamp.desc()).first()
    assert row.resource_id == str(user.id)
    # reason and operator are stripped before they are stored or echoed.
    assert json.loads(row.detail)["reason"] == "MFA reset after phishing"
    assert json.loads(row.detail)["operator"] == "ops"
    assert f"audit row {row.id}: mfa_reset {user.email}" in capsys.readouterr().out


def test_main_refuses_empty_reason_or_operator(session_local, capsys):
    argv = ["--action", "mfa_reset", "--subject", "x@example.test", "--operator", "ops"]
    assert mod.main([*argv, "--reason", "   "]) == 2
    assert mod.main([*argv, "--reason", "why", "--operator", ""]) == 2
    assert "--reason and --operator must not be empty" in capsys.readouterr().err
    assert session_local.query(AuditLog).count() == 0


def test_main_only_offers_the_breakglass_action(session_local):
    with pytest.raises(SystemExit):
        mod.main(["--action", "password_reset", "--subject", "x@example.test", "--reason", "why", "--operator", "ops"])
