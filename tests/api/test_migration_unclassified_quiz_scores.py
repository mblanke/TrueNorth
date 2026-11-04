"""9d7e2c4a6b81 permits grade-free quiz and progress rows on Unclassified."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
PARENT, REVISION = "8bc4d6fa0532", "9d7e2c4a6b81"
COLUMNS = {
    "quiz_attempts": {"score", "max_score"},
    "module_progress": {"score", "max_score"},
    "enrollments": {"final_score", "max_score"},
}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def _nullable(engine: sa.Engine) -> dict[str, bool]:
    inspector = sa.inspect(engine)
    return {
        f"{table}.{column['name']}": column["nullable"]
        for table, names in COLUMNS.items()
        for column in inspector.get_columns(table)
        if column["name"] in names
    }


@pytest.mark.slow
def test_nullable_score_migration_round_trip(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'unclassified-scores.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    # module_progress is one of the historical ORM-reconciled tables, so a fresh
    # SQLite database at an old revision is built from today's model metadata.
    assert _nullable(engine) == {
        "quiz_attempts.score": False,
        "quiz_attempts.max_score": False,
        "module_progress.score": True,
        "module_progress.max_score": True,
        "enrollments.final_score": True,
        "enrollments.max_score": True,
    }

    _alembic(url, "upgrade", REVISION)
    assert all(_nullable(engine).values())

    _alembic(url, "downgrade", PARENT)
    assert _nullable(engine) == {
        "quiz_attempts.score": False,
        "quiz_attempts.max_score": False,
        "module_progress.score": True,
        "module_progress.max_score": True,
        "enrollments.final_score": True,
        "enrollments.max_score": True,
    }

    _alembic(url, "upgrade", "head")
    assert all(_nullable(engine).values())
    engine.dispose()
