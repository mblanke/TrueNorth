"""The in-process harness (scripts/_inprocess_api.py) and MFA (v1.2.0).

Its identities carry ``tn_session="inprocess"``, which app/mfa_policy.py exempts: the
harness needs root and Docker on the host. Each user it acts as is audited once per
process (``inprocess_session``), naming the script.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from app import auth
from app.models import AuditLog, User, UserRole

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import _inprocess_api  # noqa: E402


class _Shared:
    """The test's session, handed out as SessionLocal() without being closed by callers."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


@pytest.fixture
def harness(monkeypatch, client, db_session):
    import app.db

    monkeypatch.setattr(app.db, "SessionLocal", lambda: _Shared(db_session))
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(sys, "argv", ["load_content_inprocess.py"])
    _inprocess_api._audited.clear()
    admin = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email="ops-admin@example.test",
                 display_name="Ops", role=UserRole.admin,
                 tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"))
    db_session.add(admin)
    db_session.flush()
    return admin


def test_connect_marks_the_identity_inprocess_and_audits_it(harness, db_session):
    _, api = _inprocess_api.connect(harness.email)
    status, body = api.get("/users/me")  # an admin without amr: allowed only as inprocess
    assert status == 200, body
    rows = db_session.query(AuditLog).filter_by(action="inprocess_session", resource_id=str(harness.id)).all()
    assert len(rows) == 1
    assert "load_content_inprocess.py" in rows[0].detail


def test_users_lookup_is_inprocess_and_audited_once(harness, db_session):
    users = _inprocess_api.connect_users()
    ident, role = users.lookup(harness.email)
    assert ident.tn_session == "inprocess" and role == "admin"
    users.lookup(harness.email)
    assert db_session.query(AuditLog).filter_by(action="inprocess_session").count() == 1


def test_import_app_turns_the_rate_limiter_off_unless_set(monkeypatch):
    # Each TestClient request runs on a new event loop; the limiter's async Redis client
    # stays bound to the first one and logged "attached to a different loop" on every
    # other request (seen on the TN-MGMT01 content load, 2026-10-14).
    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
    _inprocess_api._import_app()
    import os

    assert os.environ["RATE_LIMIT_ENABLED"] == "false"

    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    _inprocess_api._import_app()
    assert os.environ["RATE_LIMIT_ENABLED"] == "true"  # an explicit setting wins
