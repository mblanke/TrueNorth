"""Stored credentials are encrypted at rest and never returned.

Before: POST /hypervisors/connections wrote the vCenter password into ``password_encrypted``
exactly as typed, and ``api_token`` likewise; POST /ai-config/backends did the same with
``api_key_encrypted``. Anyone who could read the database (a backup, a replica, a support
dump) had every hypervisor and AI-engine credential. Now they are sealed with Fernet under
``TN_SECRETS_KEY`` (app/secretbox.py; the worker holds a byte-identical copy), unsealed only
where a login needs them, and an existing database is converted by migration f4b5c6d7e8a9.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from app import secretbox
from app.models import AIBackendConfig, HypervisorConnection
from app.routers import ai_config

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "control-plane" / "api"
PASSWORD = "Sup3r-Secret-vCenter!"
TOKEN = "vsphere-api-token-0d1c-secret"
AI_KEY = "sk-live-not-for-the-database"
BEFORE = "e3a4b5c6d7e8"  # the head before the sealing migration
SEALING = "f4b5c6d7e8a9"


def _conn(client, **extra) -> dict:
    body = {"name": f"vc-{uuid.uuid4().hex[:6]}", "hypervisor_type": "vsphere", "host": "vc.lab.test",
            "port": 443, "username": "svc@vsphere.local", "password": PASSWORD, "api_token": TOKEN, **extra}
    r = client.post("/hypervisors/connections", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _row(db_session, conn_id) -> HypervisorConnection:
    db_session.expire_all()
    return db_session.get(HypervisorConnection, uuid.UUID(conn_id))


# -- hypervisor connections -------------------------------------------------------------
def test_a_hypervisor_password_and_token_are_not_stored_as_typed(client, db_session):
    row = _row(db_session, _conn(client)["id"])
    for stored, plain in ((row.password_encrypted, PASSWORD), (row.api_token, TOKEN)):
        assert plain not in stored and stored.startswith(secretbox.PREFIX)
        assert secretbox.unseal(stored) == plain


def test_no_response_ever_carries_the_password_or_token(client):
    conn = _conn(client)
    bodies = [
        str(conn),
        client.get("/hypervisors/connections").text,
        client.get(f"/hypervisors/connections/{conn['id']}").text,
        client.patch(f"/hypervisors/connections/{conn['id']}", json={"notes": "x"}).text,
        client.patch(f"/hypervisors/connections/{conn['id']}", json={"password": PASSWORD}).text,
    ]
    for text in bodies:
        assert PASSWORD not in text and TOKEN not in text and secretbox.PREFIX not in text


def test_a_changed_password_and_token_are_sealed_too(client, db_session):
    conn = _conn(client)
    r = client.patch(f"/hypervisors/connections/{conn['id']}", json={"password": "N3w-Pass", "api_token": "t2"})
    assert r.status_code == 200, r.text
    row = _row(db_session, conn["id"])
    assert secretbox.unseal(row.password_encrypted) == "N3w-Pass" and "N3w-Pass" not in row.password_encrypted
    assert secretbox.unseal(row.api_token) == "t2" and row.api_token != "t2"


@respx.mock
def test_the_connection_test_logs_in_with_the_real_password(client):
    conn = _conn(client)
    session = respx.post("https://vc.lab.test/api/session").mock(return_value=httpx.Response(200, json="tok"))
    respx.get("https://vc.lab.test/api/vcenter/system/version").mock(
        return_value=httpx.Response(200, json={"version": "8.0.3"}))
    respx.get("https://vc.lab.test/api/vcenter/host").mock(return_value=httpx.Response(200, json=[]))
    respx.delete("https://vc.lab.test/api/session").mock(return_value=httpx.Response(204))
    r = client.post(f"/hypervisors/connections/{conn['id']}/test")
    assert r.status_code == 200 and r.json()["success"], r.text
    expected = "Basic " + base64.b64encode(f"svc@vsphere.local:{PASSWORD}".encode()).decode()
    assert session.calls.last.request.headers["authorization"] == expected


def test_without_a_key_credentials_are_refused_not_stored_in_clear(client, db_session, monkeypatch):
    monkeypatch.delenv("TN_SECRETS_KEY", raising=False)
    before = db_session.query(HypervisorConnection).count()
    r = client.post("/hypervisors/connections", json={
        "name": "vc", "hypervisor_type": "vsphere", "host": "h", "username": "u", "password": PASSWORD})
    assert r.status_code == 503 and "TN_SECRETS_KEY" in r.json()["detail"]
    assert PASSWORD not in r.text
    assert db_session.query(HypervisorConnection).count() == before


def test_a_connection_without_credentials_needs_no_key(client, monkeypatch):
    monkeypatch.delenv("TN_SECRETS_KEY", raising=False)
    r = client.post("/hypervisors/connections", json={
        "name": "vc", "hypervisor_type": "vsphere", "host": "h", "username": "u"})
    assert r.status_code == 201, r.text


@respx.mock
def test_a_rotated_out_key_is_a_503_and_does_not_mark_the_vcenter_down(client, db_session, monkeypatch):
    conn = _conn(client)
    session = respx.post("https://vc.lab.test/api/session").mock(return_value=httpx.Response(200, json="tok"))
    monkeypatch.setenv("TN_SECRETS_KEY", "a-completely-different-key-of-enough-length")
    r = client.post(f"/hypervisors/connections/{conn['id']}/test")
    assert r.status_code == 503 and "TN_SECRETS_KEY" in r.json()["detail"]
    assert not session.called  # never a login attempted with ciphertext
    assert _row(db_session, conn["id"]).is_active is True


# -- AI engines -------------------------------------------------------------------------
class _Engine:
    uses_fleet_nodes = False

    def __init__(self):
        self.keys: list[str | None] = []

    def probe(self, *, base_url, api_key=None, host=None, port=None):
        self.keys.append(api_key)
        return {"url": base_url, "online": False, "version": None, "models": [], "running": []}

    def generate(self, prompt, *, base_url, api_key=None, timeout=60, model=None):
        self.keys.append(api_key)
        return {"prompt": prompt, "response": "ok", "model": "m"}


def test_an_ai_engine_key_is_sealed_never_returned_and_still_used(client, db_session, monkeypatch):
    r = client.post("/ai-config/backends", json={"name": "llm", "backend_type": "openai", "is_primary": True,
                                                 "base_url": "http://llm.test", "api_key": AI_KEY})
    assert r.status_code == 201, r.text
    backend_id = r.json()["id"]
    for text in (r.text, client.get("/ai-config/backends").text, client.get(f"/ai-config/backends/{backend_id}").text):
        assert AI_KEY not in text and secretbox.PREFIX not in text
    db_session.expire_all()
    row = db_session.get(AIBackendConfig, uuid.UUID(backend_id))
    assert AI_KEY not in row.api_key_encrypted and secretbox.unseal(row.api_key_encrypted) == AI_KEY

    engine = _Engine()
    monkeypatch.setattr(ai_config, "get_ai_engine", lambda *a, **k: engine)
    assert client.post(f"/ai-config/backends/{backend_id}/discover").status_code == 200
    assert client.post("/ai-config/test-generate", params={"prompt": "hi"}).status_code == 200
    assert engine.keys and set(engine.keys) == {AI_KEY}


def test_a_changed_ai_engine_key_is_sealed(client, db_session):
    r = client.post("/ai-config/backends", json={"name": "llm", "backend_type": "openai", "base_url": "http://x"})
    assert r.status_code == 201, r.text
    assert client.patch(f"/ai-config/backends/{r.json()['id']}", json={"api_key": AI_KEY}).status_code == 200
    db_session.expire_all()
    stored = db_session.get(AIBackendConfig, uuid.UUID(r.json()["id"])).api_key_encrypted
    assert stored.startswith(secretbox.PREFIX) and secretbox.unseal(stored) == AI_KEY


# -- the box itself ---------------------------------------------------------------------
def test_sealing_round_trips_and_differs_each_time():
    a, b = secretbox.seal("x"), secretbox.seal("x")
    assert a != b and secretbox.unseal(a) == secretbox.unseal(b) == "x"
    assert secretbox.seal(None) is None and secretbox.seal("") == ""
    assert secretbox.unseal(None) is None


def test_a_wrong_key_is_an_error_not_garbage(monkeypatch):
    sealed = secretbox.seal("x")
    monkeypatch.setenv("TN_SECRETS_KEY", "a-completely-different-key-of-enough-length")
    with pytest.raises(secretbox.SecretUnreadableError):
        secretbox.unseal(sealed)


def test_a_missing_key_is_an_error_for_both_directions(monkeypatch):
    sealed = secretbox.seal("x")
    monkeypatch.delenv("TN_SECRETS_KEY")
    with pytest.raises(secretbox.SecretKeyMissingError):
        secretbox.seal("x")
    with pytest.raises(secretbox.SecretKeyMissingError):
        secretbox.unseal(sealed)


def test_keys_rotate_newest_first(monkeypatch):
    old = os.environ["TN_SECRETS_KEY"]
    sealed_old = secretbox.seal("x")
    monkeypatch.setenv("TN_SECRETS_KEY", f"a-brand-new-key-at-least-32-chars-long,{old}")
    assert secretbox.unseal(sealed_old) == "x"
    sealed_new = secretbox.seal("y")
    monkeypatch.setenv("TN_SECRETS_KEY", "a-brand-new-key-at-least-32-chars-long")
    assert secretbox.unseal(sealed_new) == "y"  # sealed with the newest key
    with pytest.raises(secretbox.SecretUnreadableError):
        secretbox.unseal(sealed_old)  # once the old key is dropped, its values are gone


def test_a_short_key_is_refused(monkeypatch):
    monkeypatch.setenv("TN_SECRETS_KEY", "too-short")
    with pytest.raises(secretbox.SecretKeyMissingError):
        secretbox.seal("x")


def test_a_value_from_before_encryption_reads_as_it_was():
    assert secretbox.unseal("legacy-plaintext") == "legacy-plaintext"


def test_the_api_and_worker_copies_are_identical():
    api = (API / "app" / "secretbox.py").read_bytes()
    worker = (ROOT / "control-plane" / "worker" / "worker" / "secretbox.py").read_bytes()
    assert api == worker, "edit control-plane/worker/worker/secretbox.py and copy it to the API"


# -- an existing database ---------------------------------------------------------------
def _alembic(db: Path, *args, key: str | None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "DATABASE_URL": f"sqlite:///{db}", "PYTHONPATH": str(API),
           "AUTH_DISABLED": "true"}
    if key:
        env["TN_SECRETS_KEY"] = key
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True,
                          text=True, timeout=300)


def _insert_plaintext(db: Path) -> None:
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO hypervisor_connections (id, name, hypervisor_type, host, port, username, "
                  "password_encrypted, api_token, verify_ssl, is_primary, is_active, created_at, updated_at) VALUES "
                  "(?, 'vc', 'vsphere', 'h', 443, 'u', ?, ?, 0, 1, 1, '2026-01-01', '2026-01-01')",
                  (uuid.uuid4().hex, PASSWORD, TOKEN))
        c.execute("INSERT INTO ai_backend_configs (id, name, backend_type, base_url, api_key_encrypted, is_primary, "
                  "is_active, max_concurrent, timeout_seconds, created_at, updated_at) VALUES "
                  "(?, 'llm', 'openai', 'http://x', ?, 1, 1, 1, 30, '2026-01-01', '2026-01-01')",
                  (uuid.uuid4().hex, AI_KEY))


def test_the_migration_seals_existing_plaintext_and_a_rollback_restores_it(tmp_path):
    db, key = tmp_path / "tn.db", os.environ["TN_SECRETS_KEY"]
    r = _alembic(db, "upgrade", BEFORE, key=key)
    assert r.returncode == 0, r.stderr[-2000:]
    _insert_plaintext(db)
    r = _alembic(db, "upgrade", SEALING, key=key)
    assert r.returncode == 0, r.stderr[-2000:]
    with sqlite3.connect(db) as c:
        pw, tok = c.execute("SELECT password_encrypted, api_token FROM hypervisor_connections").fetchone()
        (ai,) = c.execute("SELECT api_key_encrypted FROM ai_backend_configs").fetchone()
    assert PASSWORD not in pw and TOKEN not in tok and AI_KEY not in ai
    assert (secretbox.unseal(pw), secretbox.unseal(tok), secretbox.unseal(ai)) == (PASSWORD, TOKEN, AI_KEY)

    r = _alembic(db, "downgrade", BEFORE, key=key)
    assert r.returncode == 0, r.stderr[-2000:]
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT password_encrypted, api_token FROM hypervisor_connections").fetchone() == (
            PASSWORD, TOKEN)


def test_the_migration_refuses_to_run_without_a_key_when_there_is_plaintext(tmp_path):
    db = tmp_path / "tn.db"
    assert _alembic(db, "upgrade", BEFORE, key=None).returncode == 0
    _insert_plaintext(db)
    r = _alembic(db, "upgrade", SEALING, key=None)
    assert r.returncode != 0 and "TN_SECRETS_KEY" in (r.stdout + r.stderr)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT password_encrypted FROM hypervisor_connections").fetchone()[0] == PASSWORD


def test_an_empty_database_migrates_without_a_key(tmp_path):
    r = _alembic(tmp_path / "tn.db", "upgrade", "head", key=None)
    assert r.returncode == 0, r.stderr[-2000:]
