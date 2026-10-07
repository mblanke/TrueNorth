"""The worker's reads and writes of API-owned tables, as SQLAlchemy Core on ``tables.py``.

Every statement here is built from the generated mirror of the API models
(``worker/tables.py``, from ``scripts/export_worker_tables.py``), so a renamed table or
column fails at import or in ``tests/worker/test_worker_sql_real_db.py`` instead of in
production. Until MOSA slice 6b these were raw SQL strings in ``tasks.py``, and several
named tables or columns that never existed (``aars``, ``ranges.expires_at``,
``exercises.error_message``).

Each function takes an open session and does not commit: commit boundaries stay with the
caller's ``_db_session()`` block, as before. The mirror carries no Python-side defaults,
so inserts supply ids, timestamps and every NOT NULL column explicitly.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

import sqlalchemy as sa

from .tables import (
    after_action_reports,
    competencies,
    competency_assertions,
    competency_auto_assessments,
    courses,
    exercises,
    forged_exercises,
    golden_images,
    hypervisor_connections,
    learning_recommendations,
    objectives,
    range_snapshots,
    ranges,
    scenarios,
    templates,
    users,
)

_now = sa.func.now


# -- ranges -----------------------------------------------------------------
def update_range_state(
    db,
    range_id: str,
    new_state: str,
    *,
    error: str | None = None,
    output: str | None = None,
    only_from: Sequence[str] | None = None,
    clear_error: bool = False,
) -> int:
    """Set a range's state; conditional on its current state when ``only_from`` is given."""
    values: dict[str, Any] = {"state": new_state, "updated_at": _now()}
    if error:
        values["error_message"] = error
    elif clear_error:
        values["error_message"] = None
    if output:
        values["provisioner_output"] = output
    stmt = sa.update(ranges).where(ranges.c.id == range_id).values(**values)
    if only_from:
        # CAST: `state` is a native enum on Postgres and plain text on SQLite.
        stmt = stmt.where(sa.cast(ranges.c.state, sa.Text).in_(list(only_from)))
    return db.execute(stmt).rowcount


def range_template_and_backend(db, range_id: str):
    """(template yaml, provisioner_backend) for a range, or None."""
    stmt = (
        sa.select(templates.c.yaml, ranges.c.provisioner_backend)
        .select_from(ranges.join(templates, ranges.c.template_id == templates.c.id))
        .where(ranges.c.id == range_id)
    )
    return db.execute(stmt).first()


def range_output_and_backend(db, range_id: str):
    """(provisioner_output, provisioner_backend) for a range, or None."""
    stmt = sa.select(ranges.c.provisioner_output, ranges.c.provisioner_backend).where(ranges.c.id == range_id)
    return db.execute(stmt).first()


def active_ranges(db) -> list:
    """(id, name, provisioner_output) of every range that is ready or running."""
    stmt = sa.select(ranges.c.id, ranges.c.name, ranges.c.provisioner_output).where(
        ranges.c.state.in_(["ready", "running"])
    )
    return db.execute(stmt).fetchall()


def touch_range(db, range_id: str) -> None:
    db.execute(sa.update(ranges).where(ranges.c.id == range_id).values(updated_at=_now()))


def first_range_for_tenant(db, tenant_id: str):
    return db.execute(sa.select(ranges.c.id).where(ranges.c.tenant_id == tenant_id).limit(1)).first()


# -- hypervisor connections / golden images ---------------------------------
def hypervisor_connection(db, hypervisor_type: str):
    """The primary (else any) active connection for a hypervisor type, or None."""
    hc = hypervisor_connections
    stmt = (
        sa.select(
            hc.c.host, hc.c.port, hc.c.username, hc.c.password_encrypted, hc.c.api_token, hc.c.verify_ssl,
            hc.c.datacenter,
        )
        .where(hc.c.hypervisor_type == hypervisor_type, hc.c.is_active == sa.true())
        .order_by(hc.c.is_primary.desc())
        .limit(1)
    )
    return db.execute(stmt).first()


def enabled_golden_images(db, hypervisor: str) -> list:
    """(catalogue_id, template_name, os_aliases) of enabled, undeleted images for a hypervisor."""
    gi = golden_images
    stmt = sa.select(gi.c.catalogue_id, gi.c.template_name, gi.c.os_aliases).where(
        gi.c.hypervisor == hypervisor, gi.c.enabled == sa.true(), gi.c.deleted_at.is_(None)
    )
    return db.execute(stmt).fetchall()


# -- exercises + objectives --------------------------------------------------
def _exercise_scenario_join():
    return exercises.join(scenarios, exercises.c.scenario_id == scenarios.c.id)


def exercise_scenario_yaml(db, exercise_id: str):
    """(exercise id, scenario yaml), or None when the exercise or its scenario is missing."""
    stmt = (
        sa.select(exercises.c.id, scenarios.c.yaml)
        .select_from(_exercise_scenario_join())
        .where(exercises.c.id == exercise_id)
    )
    return db.execute(stmt).first()


def exercise_scores_and_yaml(db, exercise_id: str):
    """(total_score, max_score, scenario yaml), or None."""
    stmt = (
        sa.select(exercises.c.total_score, exercises.c.max_score, scenarios.c.yaml)
        .select_from(_exercise_scenario_join())
        .where(exercises.c.id == exercise_id)
    )
    return db.execute(stmt).first()


def start_exercise(db, exercise_id: str) -> None:
    db.execute(
        sa.update(exercises)
        .where(exercises.c.id == exercise_id)
        .values(state="running", started_at=_now(), updated_at=_now())
    )


LIVE_EXERCISE = ("running", "paused")


def _exercise_is_live(exercise_id: str):
    """True while the exercise is running or paused. Once an instructor has completed or
    cancelled it, results (LTI, xAPI, the AAR) may already have gone out, so a worker still
    running must not move its objectives or score."""
    # CAST: `state` is a native enum on Postgres and plain text on SQLite.
    return sa.exists().where(
        exercises.c.id == exercise_id, sa.cast(exercises.c.state, sa.Text).in_(LIVE_EXERCISE)
    )


def achieve_objective(db, exercise_id: str, ref_id: str, evidence: str | None = None) -> None:
    """Mark an objective achieved, once, while the exercise is live: an already-achieved row
    keeps its time and evidence."""
    values: dict[str, Any] = {"achieved": True, "achieved_at": _now(), "updated_at": _now()}
    if evidence is not None:
        values["evidence"] = evidence
    db.execute(
        sa.update(objectives)
        .where(
            objectives.c.exercise_id == exercise_id,
            objectives.c.ref_id == ref_id,
            objectives.c.achieved == sa.false(),
            _exercise_is_live(exercise_id),
        )
        .values(**values)
    )


def _score_values(exercise_id: str) -> dict[str, Any]:
    """total_score / max_score from the exercise's objectives (achieved points / all points)."""
    o = objectives

    def _points(*extra):
        return sa.func.coalesce(
            sa.select(sa.func.sum(o.c.points)).where(o.c.exercise_id == exercise_id, *extra).scalar_subquery(), 0
        )

    return {"total_score": _points(o.c.achieved == sa.true()), "max_score": _points()}


def running_exercises(db) -> list:
    """(id, started_at, scenario yaml or None) of every running exercise."""
    stmt = (
        sa.select(exercises.c.id, exercises.c.started_at, scenarios.c.yaml)
        .select_from(exercises.outerjoin(scenarios, exercises.c.scenario_id == scenarios.c.id))
        .where(sa.cast(exercises.c.state, sa.Text) == "running", exercises.c.started_at.is_not(None))
    )
    return db.execute(stmt).fetchall()


def complete_exercise(db, exercise_id: str) -> None:
    """Mark complete and total the score from its objectives, unless an instructor already
    completed or cancelled it (that close stands)."""
    db.execute(
        sa.update(exercises)
        .where(exercises.c.id == exercise_id, sa.cast(exercises.c.state, sa.Text).in_(LIVE_EXERCISE))
        .values(state="completed", completed_at=_now(), updated_at=_now(), **_score_values(exercise_id))
    )


def cancel_exercise(db, exercise_id: str) -> None:
    """Mark an exercise cancelled after a failed run.

    The replaced SQL also set ``error_message``, a column ``exercises`` does not have, so
    on a real database the failure handler itself raised and the state never changed.
    The error still reaches the UI through the notification the caller sends.
    """
    db.execute(sa.update(exercises).where(exercises.c.id == exercise_id).values(state="cancelled", updated_at=_now()))


def exercise_for_aar(db, exercise_id: str):
    """(id, name, state, total_score, max_score, started_at, completed_at), or None."""
    e = exercises
    stmt = sa.select(e.c.id, e.c.name, e.c.state, e.c.total_score, e.c.max_score, e.c.started_at, e.c.completed_at)
    return db.execute(stmt.where(e.c.id == exercise_id)).first()


def objectives_for_aar(db, exercise_id: str) -> list:
    """(ref_id, description, objective_type, points, achieved, evidence, achieved_at) rows."""
    o = objectives
    stmt = sa.select(
        o.c.ref_id, o.c.description, o.c.objective_type, o.c.points, o.c.achieved, o.c.evidence, o.c.achieved_at
    )
    return db.execute(stmt.where(o.c.exercise_id == exercise_id)).fetchall()


def recent_completed_exercises(db, user_id: str, limit: int = 10) -> list:
    """(name, total_score, max_score, completed_at) of the user's tenant's completed exercises, newest first."""
    e = exercises
    tenant = sa.select(users.c.tenant_id).where(users.c.id == user_id).scalar_subquery()
    stmt = (
        sa.select(e.c.name, e.c.total_score, e.c.max_score, e.c.completed_at)
        .where(e.c.tenant_id == tenant, e.c.state == "completed")
        .order_by(e.c.completed_at.desc())
        .limit(limit)
    )
    return db.execute(stmt).fetchall()


# -- after-action reports ----------------------------------------------------
def upsert_aar(db, exercise_id: str, report_json: str, report_html: str | None = None) -> str:
    """Store the exercise's AAR unless one exists; return the id of the row that holds it.

    The replaced SQL wrote to ``aars``, which does not exist (the table is
    ``after_action_reports``), so no worker-generated AAR was ever stored.

    An existing row is never overwritten: the API's ``POST /exercises/{id}/aar`` and its
    AI enhancement write the same row, and replacing an instructor's report (and its AI
    analysis) from a background task would lose work. ON CONFLICT is Postgres syntax;
    SQLite (tests) has the same form.
    """
    if db.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    inserted = db.execute(aar_upsert_statement(insert, exercise_id, report_json, report_html)).scalar_one_or_none()
    if inserted is not None:
        return str(inserted)
    existing = db.execute(
        sa.select(after_action_reports.c.id).where(after_action_reports.c.exercise_id == exercise_id)
    ).scalar_one()
    return str(existing)


def aar_upsert_statement(insert, exercise_id: str, report_json: str, report_html: str | None = None):
    """Insert-if-absent for a dialect's ``insert`` construct (postgresql.insert or sqlite.insert)."""
    stmt = insert(after_action_reports).values(
        id=str(uuid.uuid4()),
        exercise_id=exercise_id,
        report_json=report_json,
        report_html=report_html,
        generated_at=_now(),
        created_at=_now(),
        updated_at=_now(),
    )
    return stmt.on_conflict_do_nothing(index_elements=[after_action_reports.c.exercise_id]).returning(
        after_action_reports.c.id
    )


# -- snapshots ----------------------------------------------------------------
def update_snapshot_state(
    db,
    snapshot_id: str,
    new_state: str,
    *,
    data: str | None = None,
    size: int | None = None,
    only_from: Sequence[str] | None = None,
) -> int:
    """Set a snapshot's state; conditional on its current state when ``only_from`` is given."""
    s = range_snapshots
    values: dict[str, Any] = {"snapshot_state": new_state, "updated_at": _now()}
    if data is not None:
        values["snapshot_data"] = data
    if size is not None:
        values["size_bytes"] = size
    stmt = sa.update(s).where(s.c.id == snapshot_id).values(**values)
    if only_from:
        stmt = stmt.where(s.c.snapshot_state.in_(list(only_from)))
    return db.execute(stmt).rowcount


def snapshot_context(db, snapshot_id: str, range_id: str):
    """(snapshot_state, snapshot_data, range_state_at_snapshot), (state, provisioner_output, provisioner_backend)."""
    s = range_snapshots
    snap = db.execute(
        sa.select(s.c.snapshot_state, s.c.snapshot_data, s.c.range_state_at_snapshot).where(s.c.id == snapshot_id)
    ).first()
    rng = db.execute(
        sa.select(ranges.c.state, ranges.c.provisioner_output, ranges.c.provisioner_backend).where(
            ranges.c.id == range_id
        )
    ).first()
    return snap, rng


# -- forge: scenarios, exercises, forged_exercises ----------------------------
def insert_forged_scenario(db, name: str, yaml_text: str, tenant_id: str) -> str:
    """Insert the forged scenario; return its id.

    ``is_public`` is NOT NULL with no server default; the replaced SQL omitted it and the
    insert failed on any real database.
    """
    sid = str(uuid.uuid4())
    db.execute(
        sa.insert(scenarios).values(
            id=sid, name=name, version="1.0", yaml=yaml_text, tenant_id=tenant_id, is_public=False,
            created_at=_now(), updated_at=_now(),
        )
    )
    return sid


def insert_forged_exercise(db, name: str, range_id, scenario_id: str, tenant_id: str) -> str:
    """Insert a pending exercise for a forged scenario; return its id.

    ``total_score``/``max_score`` are NOT NULL with no server default, and ``kind`` has a
    server default only on Alembic-built databases; the replaced SQL omitted all three.
    """
    eid = str(uuid.uuid4())
    db.execute(
        sa.insert(exercises).values(
            id=eid, name=name, kind="assessment", range_id=range_id, scenario_id=scenario_id, state="pending",
            tenant_id=tenant_id, total_score=0, max_score=0, created_at=_now(), updated_at=_now(),
        )
    )
    return eid


def insert_forged_exercise_record(
    db,
    *,
    exercise_id: str,
    scenario_id: str,
    feed_id,
    indicator_ids: Iterable[str],
    scenario_yaml: str,
    mitre_techniques: Iterable[str],
    difficulty: str,
    model_used: str,
    tenant_id: str,
) -> str:
    fid = str(uuid.uuid4())
    db.execute(
        sa.insert(forged_exercises).values(
            id=fid, exercise_id=exercise_id, scenario_id=scenario_id, feed_id=feed_id,
            indicator_ids=json.dumps(list(indicator_ids)), scenario_yaml=scenario_yaml,
            mitre_techniques=json.dumps(list(mitre_techniques)), difficulty=difficulty, model_used=model_used,
            tenant_id=tenant_id, created_at=_now(), updated_at=_now(),
        )
    )
    return fid


# -- competencies, assertions, auto-assessments --------------------------------
def objective_competency_rows(db, exercise_id: str) -> list:
    """(competency_code, points, achieved, description, competency id or None) per coded objective."""
    o, c = objectives, competencies
    stmt = (
        sa.select(o.c.competency_code, o.c.points, o.c.achieved, o.c.description, c.c.id)
        .select_from(o.outerjoin(c, c.c.code == o.c.competency_code))
        .where(o.c.exercise_id == exercise_id, o.c.competency_code.is_not(None), o.c.competency_code != "")
    )
    return db.execute(stmt).fetchall()


def nice_competency_in_category(db, category: str):
    """(id, code, name) of one NICE competency in a category, or None."""
    c = competencies
    stmt = sa.select(c.c.id, c.c.code, c.c.name).where(c.c.framework == "nice", c.c.category == category).limit(1)
    return db.execute(stmt).first()


def insert_auto_assessment(db, user_id: str, exercise_id: str, mappings: list, raw: int, max_score: int) -> str:
    aid = str(uuid.uuid4())
    db.execute(
        sa.insert(competency_auto_assessments).values(
            id=aid, user_id=user_id, exercise_id=exercise_id, competency_mappings=json.dumps(mappings),
            raw_score=raw, max_score=max_score, assessed_at=_now(), created_at=_now(), updated_at=_now(),
        )
    )
    return aid


def upsert_competency_assertion(db, user_id: str, competency_id: str, proficiency: str, exercise_id: str) -> None:
    """Record ``exercise_id`` as evidence (last 10 kept) and set the proficiency."""
    ca = competency_assertions
    existing = db.execute(
        sa.select(ca.c.id, ca.c.evidence_refs).where(ca.c.user_id == user_id, ca.c.competency_id == competency_id)
    ).first()
    if existing:
        evidence = json.loads(existing[1]) if existing[1] else []
        evidence.append(exercise_id)
        evidence = evidence[-10:]  # Keep last 10
        db.execute(
            sa.update(ca)
            .where(ca.c.id == existing[0])
            .values(proficiency=proficiency, evidence_refs=json.dumps(evidence), assessed_at=_now(), updated_at=_now())
        )
    else:
        db.execute(
            sa.insert(ca).values(
                id=str(uuid.uuid4()), user_id=user_id, competency_id=competency_id, proficiency=proficiency,
                evidence_refs=json.dumps([exercise_id]), source="truenorth", assessed_at=_now(), created_at=_now(),
                updated_at=_now(),
            )
        )


def competency_profile(db, user_id: str) -> list:
    """(code, name, category, proficiency) of the user's assertions, most recently assessed first."""
    ca, c = competency_assertions, competencies
    stmt = (
        sa.select(c.c.code, c.c.name, c.c.category, ca.c.proficiency)
        .select_from(ca.join(c, ca.c.competency_id == c.c.id))
        .where(ca.c.user_id == user_id)
        .order_by(ca.c.assessed_at.desc())
    )
    return db.execute(stmt).fetchall()


# -- courses, learning recommendations -----------------------------------------
def published_courses(db, limit: int = 20) -> list:
    """(name, description, difficulty, nice_work_roles) of published courses."""
    c = courses
    stmt = sa.select(c.c.name, c.c.description, c.c.difficulty, c.c.nice_work_roles).where(
        c.c.is_published == sa.true()
    )
    return db.execute(stmt.limit(limit)).fetchall()


def insert_learning_recommendation(db, user_id: str, rec: dict, target_role: str, model_used: str) -> str:
    rid = str(uuid.uuid4())
    db.execute(
        sa.insert(learning_recommendations).values(
            id=rid,
            user_id=user_id,
            summary=rec.get("summary", ""),
            strengths=json.dumps(rec.get("strengths", [])),
            gaps=json.dumps(rec.get("gaps", [])),
            recommendations=json.dumps(rec.get("recommendations", [])),
            target_role=target_role or None,
            target_role_readiness=rec.get("target_role_readiness", 0.0),
            next_milestone=rec.get("next_milestone", ""),
            model_used=model_used,
            generated_at=_now(),
            created_at=_now(),
            updated_at=_now(),
        )
    )
    return rid
