"""The worker logs in to the hypervisor with the real password, of the range's own tenant.

The API stores hypervisor credentials sealed (app/secretbox.py, tests/api/test_secret_storage.py);
the worker reads the row itself (db_ops.hypervisor_creds via db_ops.hypervisor_connection) and
must unseal it with its own, byte-identical copy of the box, or every DB-registered vCenter
login fails.

Security sweep H6 (2026-10-08): the connection used to be "the primary active one" across
all tenants, so tenant B's range was built on tenant A's vCenter with A's credentials. It
is now the range's own tenant's (primary first), else a shared one with no tenant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

pytest.importorskip("worker.tasks")
from worker import db_ops, secretbox  # noqa: E402
from worker.tables import hypervisor_connections, ranges  # noqa: E402

NOW = datetime(2026, 10, 7, tzinfo=UTC)
TENANT_A, TENANT_B = str(uuid.uuid4()), str(uuid.uuid4())


@pytest.fixture
def db():
    eng = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    for table in (hypervisor_connections, ranges):
        cols = [sa.Column(c.name, c.type, primary_key=c.primary_key) for c in table.columns]  # NOT NULL relaxed
        sa.Table(table.name, meta, *cols)
    meta.create_all(eng)
    with eng.connect() as conn:
        yield conn


def _insert(db, password, token, *, tenant=TENANT_A, host="vc", primary=True):
    db.execute(hypervisor_connections.insert().values(
        id=str(uuid.uuid4()), name=host, hypervisor_type="vsphere", host=host, port=443,
        username="svc", password_encrypted=password, api_token=token, verify_ssl=False, is_primary=primary,
        is_active=True, datacenter="DC", tenant_id=tenant, created_at=NOW, updated_at=NOW))


def _range(db, tenant=TENANT_A) -> str:
    rid = str(uuid.uuid4())
    db.execute(ranges.insert().values(id=rid, tenant_id=tenant, name="r"))
    return rid


def test_sealed_credentials_are_unsealed_for_the_login(db):
    _insert(db, secretbox.seal("Sup3r-Secret"), secretbox.seal("tok"))
    creds = db_ops.hypervisor_creds(db, "vsphere", _range(db))
    assert (creds["password"], creds["api_token"]) == ("Sup3r-Secret", "tok")


def test_a_row_from_before_encryption_still_works(db):
    _insert(db, "plain", None)
    creds = db_ops.hypervisor_creds(db, "vsphere", _range(db))
    assert (creds["password"], creds["api_token"]) == ("plain", "")


def test_without_the_key_the_worker_refuses_rather_than_log_in_with_ciphertext(db, monkeypatch):
    _insert(db, secretbox.seal("Sup3r-Secret"), None)
    monkeypatch.delenv("TN_SECRETS_KEY")
    with pytest.raises(secretbox.SecretKeyMissingError):
        db_ops.hypervisor_creds(db, "vsphere", _range(db))


def test_another_tenants_primary_connection_is_never_used(db):
    _insert(db, "a-secret", None, tenant=TENANT_A, host="vc-a", primary=True)
    assert db_ops.hypervisor_creds(db, "vsphere", _range(db, TENANT_B)) == {}  # env fallback, not A's vCenter


def test_the_ranges_own_tenant_wins_over_a_primary_elsewhere(db):
    _insert(db, "a-secret", None, tenant=TENANT_A, host="vc-a", primary=True)
    _insert(db, "b-secret", None, tenant=TENANT_B, host="vc-b", primary=False)
    creds = db_ops.hypervisor_creds(db, "vsphere", _range(db, TENANT_B))
    assert (creds["host"], creds["password"]) == ("vc-b", "b-secret")


def test_a_shared_connection_serves_a_tenant_without_its_own(db):
    _insert(db, "shared", None, tenant=None, host="vc-shared", primary=False)
    _insert(db, "a-secret", None, tenant=TENANT_A, host="vc-a", primary=True)
    assert db_ops.hypervisor_creds(db, "vsphere", _range(db, TENANT_B))["host"] == "vc-shared"
    assert db_ops.hypervisor_creds(db, "vsphere", _range(db, TENANT_A))["host"] == "vc-a"
