"""9d4e7a2b5c18 adds the exercise template's two tables, and only those (ADR 0016).

Upgrade from its parent onto a database holding a template, a scenario and an exercise: the
tables appear with their keys, nothing is written, and the ORM's models are the migration's
schema. Downgrade drops both and keeps the exercise; re-upgrade restores them. SQLite here;
the PostgreSQL chain runs in CI (test_migration_populated_upgrade.py).
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
PARENT, REVISION = "7c1d2e9f4a60", "9d4e7a2b5c18"
TABLES = {"exercise_templates", "exercise_template_uses"}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def test_the_revision_chains_onto_the_single_head():
    text = (API / f"alembic/versions/{REVISION}_exercise_templates.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text
    children = [p.name for p in (API / "alembic/versions").glob("*.py")
                if f'down_revision: str | None = "{PARENT}"' in p.read_text(encoding="utf-8")]
    assert children == [f"{REVISION}_exercise_templates.py"]  # one head: nothing else chains onto the parent


@pytest.mark.slow
def test_upgrade_adds_the_tables_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path / 'et.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    from app.models import Exercise, Range, Scenario, Template
    from sqlalchemy.orm import Session

    tmpl, rng, sc, ex = (uuid.uuid4() for _ in range(4))
    with Session(engine) as db:
        db.add(Template(id=tmpl, name="Lab", version="1.0", yaml="name: x\n"))
        db.add(Scenario(id=sc, name="S", version="1.0", yaml="name: s\n"))
        db.flush()
        db.add(Range(id=rng, name="r", template_id=tmpl))
        db.flush()
        db.add(Exercise(id=ex, name="E", range_id=rng, scenario_id=sc))
        db.commit()
    assert not TABLES & set(sa.inspect(engine).get_table_names())

    _alembic(url, "upgrade", REVISION)
    insp = sa.inspect(engine)
    assert set(insp.get_table_names()) >= TABLES
    assert {c["name"] for c in insp.get_columns("exercise_templates")} >= {
        "id", "name", "revision", "tenant_id", "is_public", "scenario_id", "range_template_id", "teams",
        "objectives", "mesl", "instructor_notes", "tags", "created_by", "deleted_at"}
    assert insp.get_pk_constraint("exercise_template_uses")["constrained_columns"] == ["exercise_id"]
    with engine.connect() as db:
        for table in TABLES:
            assert db.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0, table

    from app.exercise_templates.models import ExerciseTemplate, ExerciseTemplateUse

    et = uuid.uuid4()
    with Session(engine) as db:  # the ORM's models are the migration's schema
        db.add(ExerciseTemplate(id=et, name="IR drill", description="", revision=1,
                                is_public=False, scenario_id=sc, range_template_id=tmpl, teams=[], objectives=[],
                                mesl=[], instructor_notes="", tags=[]))
        db.flush()
        db.add(ExerciseTemplateUse(exercise_id=ex, template_id=et, template_revision=1))
        db.commit()

    _alembic(url, "downgrade", PARENT)
    assert not TABLES & set(sa.inspect(engine).get_table_names())
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM exercises")).scalar() == 1

    _alembic(url, "upgrade", "head")
    assert set(sa.inspect(engine).get_table_names()) >= TABLES
    engine.dispose()
