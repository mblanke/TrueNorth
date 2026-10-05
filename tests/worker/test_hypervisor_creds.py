"""The worker logs in to the hypervisor with the real password, unsealed from the database.

The API stores hypervisor credentials sealed (app/secretbox.py, tests/api/test_secret_storage.py);
the worker reads the row itself (tasks._hypervisor_creds) and must unseal it with its own,
byte-identical copy of the box, or every DB-registered vCenter login fails.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

tasks = pytest.importorskip("worker.tasks")
from worker import secretbox  # noqa: E402


@pytest.fixture
def db():
    eng = sa.create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(sa.text(
            "CREATE TABLE hypervisor_connections (host TEXT, port INT, username TEXT, password_encrypted TEXT,"
            " api_token TEXT, verify_ssl BOOLEAN, datacenter TEXT, hypervisor_type TEXT, is_active BOOLEAN,"
            " is_primary BOOLEAN)"))
    with eng.connect() as conn:
        yield conn


def test_sealed_credentials_are_unsealed_for_the_login(db):
    db.execute(sa.text("INSERT INTO hypervisor_connections VALUES ('vc', 443, 'svc', :p, :t, 0, 'DC', 'vsphere', 1, 1)"),
               {"p": secretbox.seal("Sup3r-Secret"), "t": secretbox.seal("tok")})
    creds = tasks._hypervisor_creds(db, "vsphere")
    assert (creds["password"], creds["api_token"]) == ("Sup3r-Secret", "tok")


def test_a_row_from_before_encryption_still_works(db):
    db.execute(sa.text("INSERT INTO hypervisor_connections VALUES ('vc', 443, 'svc', 'plain', NULL, 0, NULL, 'vsphere', 1, 1)"))
    assert tasks._hypervisor_creds(db, "vsphere")["password"] == "plain"
