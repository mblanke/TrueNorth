# Author: Marc-Andre Blanke
# Created: 2026-11-19T12:30:00Z
"""d6f8b0a2c4e5 adds one index, and only that (audit #443 f34).

Upgrade from its parent adds the partial unique index ``uq_imported_result_first_per_attempt``
on ``imported_results``; rows already there are untouched, and corrections (which keep the
attempt id and name a predecessor) are outside it. If two first results of one attempt
already exist, the upgrade refuses and names them. Downgrade drops the index. SQLite here
(foreign keys are not enforced, so the rows carry made-up parents); the PostgreSQL chain runs
in CI (test_migration_populated_upgrade.py with TEST_POSTGRES_ADMIN_URL).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
PARENT, REVISION = "c5e7a9b1d3f4", "d6f8b0a2c4e5"
INDEX = "uq_imported_result_first_per_attempt"


def _alembic(url: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True, timeout=300
    )
    if check:
        assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
    return done


def _indexes(url: str) -> set[str]:
    return {i["name"] for i in sa.inspect(sa.create_engine(url)).get_indexes("imported_results")}


def _insert(url: str, *, tenant: uuid.UUID, attempt: uuid.UUID, supersedes: uuid.UUID | None = None) -> uuid.UUID:
    package = uuid.uuid4()
    with sa.create_engine(url).begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO imported_results (package_id, package_sha256, tenant_id, subject_binding_id, user_id,"
                " source_enclave, course_release_id, assessment_id, attempt_id, raw_score, max_score, outcome,"
                " scorer_version, evidence_sha256_json, supersedes_package_id, occurred_at)"
                " VALUES (:p, :h, :t, :b, :u, 'unclass', :r, 'final', :a, 9, 10, 'pass', 'v1', '[]', :s,"
                " '2026-11-19 12:00:00')"
            ),
            {
                "p": package.hex,
                "h": "a" * 64,
                "t": tenant.hex,
                "b": uuid.uuid4().hex,
                "u": uuid.uuid4().hex,
                "r": uuid.uuid4().hex,
                "a": attempt.hex,
                "s": supersedes.hex if supersedes else None,
            },
        )
    return package


def test_the_revision_chains_onto_the_previous_head():
    text = (API / f"alembic/versions/{REVISION}_imported_results_one_first_result.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_upgrade_adds_the_index_keeps_rows_and_downgrade_removes_it(tmp_path):
    url = f"sqlite:///{tmp_path / 'first.db'}"
    _alembic(url, "upgrade", PARENT)
    assert INDEX not in _indexes(url)
    tenant, attempt = uuid.uuid4(), uuid.uuid4()
    first = _insert(url, tenant=tenant, attempt=attempt)
    _alembic(url, "upgrade", REVISION)
    assert INDEX in _indexes(url)
    with sa.create_engine(url).connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM imported_results")).scalar() == 1
    # a correction keeps the attempt id and names its predecessor: allowed
    _insert(url, tenant=tenant, attempt=attempt, supersedes=first)
    # a second first result for the same attempt: refused by the database
    with pytest.raises(sa.exc.IntegrityError):
        _insert(url, tenant=tenant, attempt=attempt)
    # the same attempt id under another tenant is its own attempt
    _insert(url, tenant=uuid.uuid4(), attempt=attempt)
    _alembic(url, "downgrade", PARENT)
    assert INDEX not in _indexes(url)
    _alembic(url, "upgrade", REVISION)
    assert INDEX in _indexes(url)


@pytest.mark.slow
def test_upgrade_refuses_and_names_two_first_results_of_one_attempt(tmp_path):
    url = f"sqlite:///{tmp_path / 'dupes.db'}"
    _alembic(url, "upgrade", PARENT)
    tenant, attempt = uuid.uuid4(), uuid.uuid4()
    _insert(url, tenant=tenant, attempt=attempt)
    _insert(url, tenant=tenant, attempt=attempt)
    done = _alembic(url, "upgrade", REVISION, check=False)
    assert done.returncode != 0
    assert "more than one first result" in done.stderr + done.stdout
    assert INDEX not in _indexes(url)
