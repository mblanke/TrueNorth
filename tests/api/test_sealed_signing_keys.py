"""The LTI tool private key and the noise plan keys are sealed at rest.

f4b5c6d7e8a9 sealed hypervisor and AI-engine credentials but left two signing secrets as
stored: ``lti_tool_keys.private_key_pem`` (signs every LTI, AGS, Moodle SSO and lab-access
token, so a database read meant forging any of them) and ``noise_profiles.plan_key``
(signs noise ground truth). Both are now sealed with app/secretbox.py; migration
b7c8d9e0f1a3 converts an existing database and its downgrade restores it.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import jwt
import pytest
from app import lti13, secretbox
from app.models import LTIToolKey, Range, Template
from app.noise.models import NoiseProfile
from sqlalchemy import text

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
BEFORE = "f4b5c6d7e8a9"
SEALING = "b7c8d9e0f1a3"
DEV_TENANT = "00000000-0000-0000-0000-000000000001"


def test_a_new_tool_key_is_stored_sealed_and_still_signs(db_session):
    db_session.query(LTIToolKey).delete()
    key = lti13.get_tool_key(db_session)
    stored = db_session.execute(text("SELECT private_key_pem FROM lti_tool_keys")).scalar_one()
    assert stored.startswith(secretbox.PREFIX) and "PRIVATE KEY" not in stored
    pem = lti13.signing_pem(key)
    assert pem.startswith("-----BEGIN PRIVATE KEY-----")
    token = jwt.encode({"sub": "x"}, pem, algorithm="RS256")
    assert jwt.decode(token, key.public_key_pem, algorithms=["RS256"])["sub"] == "x"


def test_a_key_stored_before_sealing_still_signs(db_session):
    db_session.query(LTIToolKey).delete()
    legacy_pem = lti13.signing_pem(lti13.get_tool_key(db_session))
    key = db_session.query(LTIToolKey).one()
    key.private_key_pem = legacy_pem  # as a row from before the migration holds it
    db_session.flush()
    assert lti13.signing_pem(key) == legacy_pem


def test_without_the_secrets_key_no_tool_key_is_minted_in_clear(db_session, monkeypatch):
    db_session.query(LTIToolKey).delete()
    monkeypatch.delenv("TN_SECRETS_KEY")
    with pytest.raises(secretbox.SecretKeyMissingError):
        lti13.get_tool_key(db_session)


def test_a_plan_key_is_sealed_in_the_database_and_plain_in_code(db_session):
    tpl = Template(name="T", version="1.0", yaml="nodes: []", tenant_id=DEV_TENANT)
    db_session.add(tpl)
    db_session.flush()
    rng = Range(name="R", template_id=tpl.id, tenant_id=DEV_TENANT, state="ready")
    db_session.add(rng)
    db_session.flush()
    prof = NoiseProfile(range_id=rng.id, tenant_id=DEV_TENANT)
    db_session.add(prof)
    db_session.flush()
    plain = prof.plan_key
    assert len(plain) == 64

    stored = db_session.execute(text("SELECT plan_key FROM noise_profiles")).scalars().all()
    assert stored and all(s.startswith(secretbox.PREFIX) for s in stored)
    assert plain not in stored

    db_session.expire_all()
    assert db_session.get(NoiseProfile, prof.id).plan_key == plain


# -- an existing database ---------------------------------------------------------------
def _alembic(db: Path, *args, key: str | None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "DATABASE_URL": f"sqlite:///{db}", "PYTHONPATH": str(API),
           "AUTH_DISABLED": "true"}
    if key:
        env["TN_SECRETS_KEY"] = key
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True,
                          text=True, timeout=300)


PEM = "-----BEGIN PRIVATE KEY-----\nlegacy\n-----END PRIVATE KEY-----\n"
PLAN = "ab" * 32


def _insert_plaintext(db: Path) -> None:
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO lti_tool_keys (id, kid, private_key_pem, public_key_pem, is_active) "
                  "VALUES (?, 'k1', ?, 'pub', 1)", (uuid.uuid4().hex, PEM))
        c.execute("INSERT INTO noise_profiles (id, range_id, plan_key) VALUES (?, ?, ?)",
                  (uuid.uuid4().hex, uuid.uuid4().hex, PLAN))


def _stored(db: Path) -> tuple[str, str]:
    with sqlite3.connect(db) as c:
        return (c.execute("SELECT private_key_pem FROM lti_tool_keys").fetchone()[0],
                c.execute("SELECT plan_key FROM noise_profiles").fetchone()[0])


def test_the_migration_seals_both_and_a_rollback_restores_them(tmp_path):
    db, key = tmp_path / "tn.db", os.environ["TN_SECRETS_KEY"]
    r = _alembic(db, "upgrade", BEFORE, key=key)
    assert r.returncode == 0, r.stderr[-2000:]
    _insert_plaintext(db)

    r = _alembic(db, "upgrade", SEALING, key=key)
    assert r.returncode == 0, r.stderr[-2000:]
    pem, plan = _stored(db)
    assert PEM not in pem and PLAN not in plan
    assert (secretbox.unseal(pem), secretbox.unseal(plan)) == (PEM, PLAN)
    assert len(plan) > 64  # the column was widened to hold it

    r = _alembic(db, "downgrade", BEFORE, key=key)
    assert r.returncode == 0, r.stderr[-2000:]
    assert _stored(db) == (PEM, PLAN)


def test_the_migration_refuses_to_run_without_a_key_when_there_is_plaintext(tmp_path):
    db = tmp_path / "tn.db"
    assert _alembic(db, "upgrade", BEFORE, key=None).returncode == 0
    _insert_plaintext(db)
    r = _alembic(db, "upgrade", SEALING, key=None)
    assert r.returncode != 0 and "TN_SECRETS_KEY" in (r.stdout + r.stderr)
    assert _stored(db) == (PEM, PLAN)
