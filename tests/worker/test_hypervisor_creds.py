"""The worker logs in to the hypervisor with the real password, unsealed from the database.

The API stores hypervisor credentials sealed (app/secretbox.py, tests/api/test_secret_storage.py);
the worker reads the row itself (tasks._hypervisor_creds via db_ops.hypervisor_connection) and
must unseal it with its own, byte-identical copy of the box, or every DB-registered vCenter
login fails.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

tasks = pytest.importorskip("worker.tasks")
from worker import secretbox  # noqa: E402
from worker.tables import hypervisor_connections  # noqa: E402

NOW = datetime(2026, 10, 7, tzinfo=UTC)


@pytest.fixture
def db():
    eng = sa.create_engine("sqlite://")
    hypervisor_connections.metadata.create_all(eng, tables=[hypervisor_connections])
    with eng.connect() as conn:
        yield conn


def _insert(db, password, token):
    db.execute(hypervisor_connections.insert().values(
        id="00000000-0000-0000-0000-0000000000aa", name="vc", hypervisor_type="vsphere", host="vc", port=443,
        username="svc", password_encrypted=password, api_token=token, verify_ssl=False, is_primary=True,
        is_active=True, datacenter="DC", created_at=NOW, updated_at=NOW))


def test_sealed_credentials_are_unsealed_for_the_login(db):
    _insert(db, secretbox.seal("Sup3r-Secret"), secretbox.seal("tok"))
    creds = tasks._hypervisor_creds(db, "vsphere")
    assert (creds["password"], creds["api_token"]) == ("Sup3r-Secret", "tok")


def test_a_row_from_before_encryption_still_works(db):
    _insert(db, "plain", None)
    creds = tasks._hypervisor_creds(db, "vsphere")
    assert (creds["password"], creds["api_token"]) == ("plain", "")


def test_without_the_key_the_worker_refuses_rather_than_log_in_with_ciphertext(db, monkeypatch):
    _insert(db, secretbox.seal("Sup3r-Secret"), None)
    monkeypatch.delenv("TN_SECRETS_KEY")
    with pytest.raises(secretbox.SecretKeyMissingError):
        tasks._hypervisor_creds(db, "vsphere")
