"""The worker's SQL, executed against the API's own schema.

Every other worker test mocks ``_db_session``, so until MOSA slice 6b none of the
worker's SQL had ever run in a test, and several statements named tables or columns
that do not exist (``aars``, ``ranges.expires_at``, ``exercises.error_message``) or
left NOT NULL columns empty. Here each statement group runs through the real task code
against in-memory SQLite created from ``app.models`` (``Base.metadata.create_all``), and
the tests assert the rows the API would read.

SQLite stands in for Postgres. The one Postgres-only construct (the AAR upsert's
``ON CONFLICT``) runs through SQLite's identical syntax here and is compiled for the
Postgres dialect in ``TestPostgresRendering``, which also renders every helper's
statements for Postgres.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app import models as m
from app.db import Base
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

tasks = pytest.importorskip("worker.tasks")
from worker import db_ops, exercise_clock  # noqa: E402
from worker.provisioners.results import (  # noqa: E402
    DestroyResult,
    HealthResult,
    ProvisionResult,
    RestoreResult,
    SnapshotDeleteResult,
    SnapshotResult,
)


# -- fixtures -------------------------------------------------------------------
@pytest.fixture
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def notify():
    return MagicMock()


@pytest.fixture
def worker_db(engine, notify, monkeypatch):
    """Patch the worker's session factory to a real Session on ``engine``; return a reader session."""
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    @contextmanager
    def _session():  # same commit/rollback contract as tasks._db_session
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(tasks, "_db_session", _session)
    monkeypatch.setattr(tasks, "_notify_api", notify)
    monkeypatch.setattr(tasks.time, "sleep", lambda *_: None)
    reader = factory()
    yield reader
    reader.close()


def _add(db: Session, *objs):
    db.add_all(objs)
    db.commit()
    return objs[0] if len(objs) == 1 else objs


def _fresh(db: Session, obj):
    db.expire_all()
    return db.get(type(obj), obj.id)


@pytest.fixture
def world(worker_db):
    """A tenant, template, range, scenario and exercise as the API would create them."""
    db = worker_db
    tenant = _add(db, m.Tenant(name="t1", slug="t1"))
    tmpl = _add(db, m.Template(name="tpl", yaml="name: tpl\n", tenant_id=tenant.id))
    rng = _add(
        db,
        m.Range(name="r1", template_id=tmpl.id, tenant_id=tenant.id, provisioner_backend="mock",
                state=m.RangeState.created),
    )
    scen = _add(
        db,
        m.Scenario(name="s1", yaml="timeline:\n  - t: '0:01'\n    action: phish\n# T1566 T1059\n",
                   tenant_id=tenant.id),
    )
    ex = _add(db, m.Exercise(name="e1", range_id=rng.id, scenario_id=scen.id, tenant_id=tenant.id))
    user = _add(
        db,
        m.User(keycloak_id="kc-1", email="s@example.org", display_name="Student One", tenant_id=tenant.id),
    )
    return SimpleNamespace(db=db, tenant=tenant, template=tmpl, range=rng, scenario=scen, exercise=ex, user=user)


def _fake_backend(**methods):
    backend = MagicMock()
    for name, value in methods.items():
        setattr(backend, name, AsyncMock(return_value=value))
    return backend


# -- ranges, templates, golden images, hypervisor connections --------------------
class TestRanges:
    def test_update_range_state_writes_state_output_and_error(self, world):
        rid = str(world.range.id)
        tasks._update_range_state(rid, "failed", error="boom", output='{"vms": []}')
        r = _fresh(world.db, world.range)
        assert (r.state, r.error_message, r.provisioner_output) == (m.RangeState.failed, "boom", '{"vms": []}')
        assert r.updated_at is not None

        tasks._update_range_state(rid, "ready", clear_error=True)
        r = _fresh(world.db, world.range)
        assert (r.state, r.error_message) == (m.RangeState.ready, None)

    def test_only_from_guard_on_the_enum_column(self, world):
        rid = str(world.range.id)
        tasks._update_range_state(rid, "destroyed")
        tasks._update_range_state(rid, "failed", error="late restore", only_from=tasks._RESTORABLE_STATES)
        assert _fresh(world.db, world.range).state == m.RangeState.destroyed
        with tasks._db_session() as db:
            assert db_ops.update_range_state(db, rid, "ready", only_from=("destroyed",)) == 1
        assert _fresh(world.db, world.range).state == m.RangeState.ready

    def test_provision_reads_template_golden_images_and_credentials(self, world, monkeypatch):
        db = world.db
        world.template.yaml = (
            "nodes:\n  - id: web\n    os: house-ubuntu\n    vlan: lan\n  - id: old\n    os: win-2008\n    vlan: lan\n"
            "network:\n  vlans:\n    - name: lan\n      cidr: 10.9.0.0/24\n"
        )
        world.range.provisioner_backend = "vsphere_api"
        _add(
            db,
            m.GoldenImage(catalogue_id="ubuntu-2404", hypervisor="vsphere", template_name="tpl-ubuntu",
                          os_aliases='["house-ubuntu"]'),
            m.GoldenImage(catalogue_id="win-2008", hypervisor="vsphere", template_name="disabled-tpl",
                          enabled=False),
            m.GoldenImage(catalogue_id="win-2008-del", hypervisor="vsphere", template_name="deleted-tpl",
                          os_aliases='["win-2008"]', deleted_at=datetime.now(UTC)),
            m.HypervisorConnection(name="backup", hypervisor_type="vsphere", host="vc-b", port=443,
                                   username="u2", is_primary=False, is_active=True),
            m.HypervisorConnection(name="main", hypervisor_type="vsphere", host="vc-a", port=443, username="u1",
                                   password_encrypted="pw", is_primary=True, is_active=True, datacenter="DC1"),
        )
        backend = _fake_backend(provision=ProvisionResult(status="ok", vms=[{"name": "web"}], networks=[]))
        monkeypatch.setattr(tasks, "_get_backend", lambda name=None: backend)

        out = tasks.provision_range(str(world.range.id))

        assert out == {"status": "ready", "range_id": str(world.range.id), "vm_count": 1}
        template = backend.provision.await_args.args[1]
        assert template["credentials"]["host"] == "vc-a"  # primary first
        assert template["credentials"]["datacenter"] == "DC1"
        by_name = {vm["node_id"]: vm["template_name"] for vm in template["vms"]}
        assert by_name["web"] == "tpl-ubuntu"  # resolved through os_aliases
        assert by_name["old"] == "win-2008"  # disabled and soft-deleted images are not candidates: unresolved
        r = _fresh(db, world.range)
        assert r.state == m.RangeState.ready
        assert json.loads(r.provisioner_output)["provider"] == "vsphere_api"

    def test_provision_failure_marks_failed(self, world, monkeypatch):
        backend = _fake_backend(provision=ProvisionResult(status="failed", errors=["no capacity"]))
        monkeypatch.setattr(tasks, "_get_backend", lambda name=None: backend)
        with pytest.raises(RuntimeError, match="no capacity"):
            tasks.provision_range(str(world.range.id))
        r = _fresh(world.db, world.range)
        assert (r.state, r.error_message) == (m.RangeState.failed, "no capacity")

    def test_destroy_reads_output_and_backend(self, world, monkeypatch):
        world.range.provisioner_output = '{"vms": [{"name": "web"}]}'
        world.range.state = m.RangeState.ready
        world.db.commit()
        backend = _fake_backend(destroy=DestroyResult(status="ok", resources_removed=1))
        seen = []
        monkeypatch.setattr(tasks, "_get_backend", lambda name=None: seen.append(name) or backend)

        assert tasks.destroy_range(str(world.range.id))["status"] == "destroyed"
        assert seen == ["mock"]
        assert backend.destroy.await_args.args[1] == {"vms": [{"name": "web"}]}
        assert _fresh(world.db, world.range).state == m.RangeState.destroyed

    def test_health_check_and_metrics_read_active_ranges(self, world, monkeypatch):
        db = world.db
        world.range.state = m.RangeState.ready
        world.range.provisioner_output = '{"vms": [{"name": "a"}]}'
        stopped = m.Range(name="r2", template_id=world.template.id, state=m.RangeState.stopped)
        _add(db, stopped)
        before = _fresh(db, world.range).updated_at
        backend = _fake_backend(health_check=HealthResult(healthy=False, status="degraded", vm_statuses=[{}]))
        monkeypatch.setattr(tasks, "_get_backend", lambda name=None: backend)

        summary = tasks.health_check_ranges()
        assert summary["checked"] == 1
        assert summary["unhealthy_ids"] == [str(world.range.id)]  # a str, JSON-serialisable

        with patch.object(tasks.ingest_telemetry_batch, "delay") as delay:
            assert tasks.collect_range_metrics()["ranges_collected"] == 1
        assert delay.call_args.args[0] == str(world.range.id)
        assert _fresh(db, world.range).updated_at >= before


# -- exercises + objectives ----------------------------------------------------------
def _objective(ex, ref, points, **kw):
    return m.Objective(exercise_id=ex.id, ref_id=ref, objective_type=m.ObjectiveType.detection,
                       description=f"objective {ref}", validator="manual", points=points, **kw)


class TestExercisesAndObjectives:
    def test_run_scenario_reads_the_scenario_timeline(self, world):
        out = tasks.run_scenario(str(world.exercise.id))
        assert out == {"status": "completed", "exercise_id": str(world.exercise.id), "events_executed": 1}

    def test_run_scenario_without_a_scenario_reports_not_found(self, world):
        world.exercise.scenario_id = None
        world.db.commit()
        assert tasks.run_scenario(str(world.exercise.id))["status"] == "error"

    def test_run_scenario_v2_starts_achieves_and_scores(self, world, monkeypatch):
        monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
        ex = world.exercise
        other = _add(world.db, m.Exercise(name="e2", range_id=world.range.id, tenant_id=world.tenant.id))
        _add(world.db, _objective(ex, "o1", 30), _objective(ex, "o2", 70), _objective(other, "o1", 999))
        definition = {"timeline": [{"t": "0:01", "action": "phish"}], "objectives": [{"ref_id": "o1"}]}

        out = tasks.run_scenario_v2(str(ex.id), definition)

        assert out["objectives_completed"] == 1
        e = _fresh(world.db, ex)
        assert e.state == m.ExerciseState.completed
        assert e.started_at is not None and e.completed_at is not None
        # COALESCE(SUM(achieved points)) / COALESCE(SUM(all points)), this exercise only
        assert (e.total_score, e.max_score) == (30, 100)
        achieved = world.db.scalars(select(m.Objective).where(m.Objective.achieved.is_(True))).all()
        assert [(o.exercise_id, o.ref_id) for o in achieved] == [(ex.id, "o1")]
        assert achieved[0].achieved_at is not None
        assert _fresh(world.db, other).total_score == 0

    def test_run_scenario_v2_with_no_objectives_scores_zero_not_null(self, world, monkeypatch):
        monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
        tasks.run_scenario_v2(str(world.exercise.id), {"timeline": []})
        e = _fresh(world.db, world.exercise)
        assert (e.state, e.total_score, e.max_score) == (m.ExerciseState.completed, 0, 0)

    def test_run_scenario_v2_failure_cancels_the_exercise(self, world, notify, monkeypatch):
        # Production defect: the failure handler also set exercises.error_message, which does
        # not exist, so it raised in turn and the exercise stayed `running` forever.
        monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
        with pytest.raises(ValueError):
            tasks.run_scenario_v2(str(world.exercise.id), {"timeline": [{"t": "x:y"}]})
        assert _fresh(world.db, world.exercise).state == m.ExerciseState.cancelled
        assert notify.call_args.args[1]["state"] == "failed"


# -- a real exercise stays live; the clock closes it (ADR 0005 §6) ---------------------
def _detection(ex, ref, points, query="event_type:email"):
    return m.Objective(exercise_id=ex.id, ref_id=ref, objective_type=m.ObjectiveType.detection,
                       description=f"detect {ref}", validator="opensearch_query",
                       validator_params=json.dumps({"query": query}), points=points)


class TestRealBackendExercise:
    def test_timeline_end_leaves_it_running_and_achieves_nothing(self, world, monkeypatch, notify):
        # The blocker: the run used to score the attack's own telemetry and then close the
        # exercise seconds later. Credit now comes only from Student detections (API).
        monkeypatch.setenv("PROVISIONER_BACKEND", "vsphere_api")
        _add(world.db, _detection(world.exercise, "phish", 10))

        out = tasks.run_scenario_v2(str(world.exercise.id), {"timeline": [{"t": "0:00"}], "objectives": []})

        assert out == {"status": "running", "exercise_id": str(world.exercise.id), "events_executed": 1}
        e = _fresh(world.db, world.exercise)
        assert (e.state, e.total_score, e.completed_at) == (m.ExerciseState.running, 0, None)
        assert world.db.scalars(select(m.Objective.achieved)).one() is False
        assert notify.call_args.args[1]["phase"] == "timeline_complete"

    @pytest.mark.parametrize("closed", [m.ExerciseState.completed, m.ExerciseState.cancelled])
    def test_a_closed_exercise_keeps_its_objectives_score_and_state(self, world, closed):
        # Review finding 6: results (LTI, xAPI, AAR) may have gone out when an instructor
        # closed it; a worker still running must not move them.
        ex = world.exercise
        ex.state, ex.total_score = closed, 0
        _add(world.db, _detection(ex, "phish", 10))
        with tasks._db_session() as db:
            db_ops.achieve_objective(db, str(ex.id), "phish", evidence="late")
            db_ops.complete_exercise(db, str(ex.id))
        assert world.db.scalars(select(m.Objective.achieved)).one() is False
        e = _fresh(world.db, ex)
        assert (e.state, e.total_score) == (closed, 0)


class TestExerciseClock:
    def _running(self, world, minutes_ago, yaml_text):
        ex = world.exercise
        world.scenario.yaml = yaml_text
        ex.state, ex.started_at = m.ExerciseState.running, datetime.now(UTC) - timedelta(minutes=minutes_ago)
        world.db.commit()
        return ex

    def test_an_exercise_past_its_duration_is_completed_and_scored(self, world, notify):
        ex = self._running(world, 91, "duration_minutes: 90\n")
        _add(world.db, _detection(ex, "phish", 10), _detection(ex, "c2", 30))
        world.db.execute(m.Objective.__table__.update().where(m.Objective.ref_id == "c2").values(achieved=True))
        world.db.commit()

        assert exercise_clock.close_overdue_exercises() == {"completed": [str(ex.id)]}

        e = _fresh(world.db, ex)
        assert (e.state, e.total_score, e.max_score) == (m.ExerciseState.completed, 30, 40)
        assert notify.call_args.args[1] == {"id": str(ex.id), "state": "completed", "reason": "duration elapsed"}

    @pytest.mark.parametrize(
        ("minutes_ago", "yaml_text"),
        [(89, "duration_minutes: 90\n"), (500, "name: no duration\n"), (500, "duration_min: 0\n"), (500, "[")],
    )
    def test_still_running_when_time_is_left_or_no_duration_is_set(self, world, minutes_ago, yaml_text):
        ex = self._running(world, minutes_ago, yaml_text)
        assert exercise_clock.close_overdue_exercises() == {"completed": []}
        assert _fresh(world.db, ex).state == m.ExerciseState.running

    def test_duration_min_spelling_counts(self, world):
        ex = self._running(world, 31, "duration_min: 30\n")
        assert exercise_clock.close_overdue_exercises() == {"completed": [str(ex.id)]}


# -- after-action reports --------------------------------------------------------------
class TestAfterActionReports:
    def test_generate_aar_stores_the_api_report_shape(self, world, monkeypatch):
        monkeypatch.delenv("AI_ORCHESTRATOR_URL", raising=False)
        ex = world.exercise
        ex.state, ex.total_score, ex.max_score = m.ExerciseState.completed, 30, 100
        world.db.commit()
        _add(world.db, _objective(ex, "o1", 30, achieved=True, evidence="alert 42"), _objective(ex, "o2", 70))

        first = tasks.generate_aar(str(ex.id))
        rows = world.db.scalars(select(m.AfterActionReport)).all()
        assert len(rows) == 1
        aar = rows[0]
        assert first["aar_id"] == str(aar.id)
        assert aar.generated_at is not None and aar.created_at is not None
        report = json.loads(aar.report_json)
        # The keys the API's PDF/HTML views read (routers/exercises.py: data["exercise"], data["scores"]).
        assert report["exercise"]["name"] == "e1" and report["exercise"]["state"] == "completed"
        assert report["scores"] == {"total": 30, "max": 100, "pct": 30.0}
        assert {o["ref_id"]: o["type"] for o in report["objectives"]} == {"o1": "detection", "o2": "detection"}
        assert "Score: 30.0%" in aar.report_html

        # A second run must not replace the stored report; it reports the stored row's id.
        world.exercise.total_score = 100
        world.db.commit()
        second = tasks.generate_aar(str(ex.id))
        world.db.expire_all()
        rows = world.db.scalars(select(m.AfterActionReport)).all()
        assert len(rows) == 1
        assert second["aar_id"] == first["aar_id"]
        assert json.loads(rows[0].report_json)["scores"]["total"] == 30

    def test_generate_aar_never_overwrites_an_api_generated_report(self, world, monkeypatch):
        """Adversarial review: replacing an instructor's (AI-enhanced) report loses work."""
        monkeypatch.delenv("AI_ORCHESTRATOR_URL", raising=False)
        api_report = json.dumps({"exercise": {"name": "e1"}, "scores": {"total": 85}, "ai_analysis": {"x": 1}})
        existing = _add(
            world.db,
            m.AfterActionReport(exercise_id=world.exercise.id, report_json=api_report, report_html="<p>api</p>"),
        )
        out = tasks.generate_aar(str(world.exercise.id))
        world.db.expire_all()
        row = world.db.scalars(select(m.AfterActionReport)).one()
        assert out["aar_id"] == str(existing.id)
        assert (row.report_json, row.report_html) == (api_report, "<p>api</p>")

    def test_generate_aar_for_a_missing_exercise_fails(self, world, notify):
        with pytest.raises(ValueError, match="not found"):
            tasks.generate_aar(str(uuid.uuid4()))
        assert notify.call_args.args[1]["event"] == "aar_failed"
        assert world.db.scalars(select(m.AfterActionReport)).all() == []


# -- range snapshots ----------------------------------------------------------------------
@pytest.fixture
def snap(world):
    world.range.state = m.RangeState.ready
    world.range.provisioner_output = '{"vms": [{"name": "web", "vmid": 101}]}'
    world.db.commit()
    return _add(
        world.db,
        m.RangeSnapshot(range_id=world.range.id, name="before", range_state_at_snapshot="ready",
                        tenant_id=world.tenant.id),
    )


class TestSnapshots:
    def test_snapshot_range_stores_data_and_size_on_the_api_row(self, world, snap, monkeypatch):
        backend = _fake_backend(
            snapshot=SnapshotResult(status="ok", vms_snapped=1),
            delete_snapshot=SnapshotDeleteResult(status="ok"),
        )
        monkeypatch.setattr(tasks, "_get_backend", lambda name=None: backend)

        out = tasks.snapshot_range(str(world.range.id), str(snap.id))

        assert out == {"status": "ready", "snapshot_id": str(snap.id)}
        s = _fresh(world.db, snap)
        assert s.snapshot_state == "ready"
        assert json.loads(s.snapshot_data)["snapshot_name"] == tasks._backend_snapshot_name(str(snap.id))
        assert s.size_bytes == 0

    def test_a_snapshot_deleted_meanwhile_is_not_revived(self, world, snap):
        sid = str(snap.id)
        assert tasks._update_snapshot_state(sid, "deleted") == 1
        assert tasks._update_snapshot_state(sid, "ready", data="{}", size=5, only_from=tasks._SNAPSHOT_PENDING) == 0
        s = _fresh(world.db, snap)
        assert (s.snapshot_state, s.snapshot_data, s.size_bytes) == ("deleted", None, 0)

    def test_snapshot_context_reads_both_rows(self, world, snap):
        s, r = tasks._snapshot_context(str(snap.id), str(world.range.id))
        assert tuple(s) == ("creating", None, "ready")
        assert (r[0], r[2]) == ("ready", "mock")
        assert tasks._snapshot_context(str(uuid.uuid4()), str(uuid.uuid4())) == (None, None)

    def test_restore_returns_range_and_snapshot_to_ready(self, world, snap, monkeypatch):
        snap.snapshot_state, snap.snapshot_data = "restoring", '{"snapshot_name": "tnabc"}'
        world.range.state, world.range.error_message = m.RangeState.failed, "old failure"
        world.db.commit()
        backend = _fake_backend(restore=RestoreResult(status="ok", vms_reverted=1, vms_restored=1))
        monkeypatch.setattr(tasks, "_get_backend", lambda name=None: backend)

        assert tasks.restore_snapshot(str(world.range.id), str(snap.id))["status"] == "restored"
        assert backend.restore.await_args.args[2] == "tnabc"
        r = _fresh(world.db, world.range)
        assert (r.state, r.error_message) == (m.RangeState.ready, None)
        assert _fresh(world.db, snap).snapshot_state == "ready"

    def test_restore_leaves_a_destroyed_range_alone(self, world, snap, monkeypatch):
        snap.snapshot_state, snap.snapshot_data = "restoring", '{"snapshot_name": "tnabc"}'
        world.range.state = m.RangeState.destroyed
        world.db.commit()
        backend = _fake_backend()
        monkeypatch.setattr(tasks, "_get_backend", lambda name=None: backend)

        assert tasks.restore_snapshot(str(world.range.id), str(snap.id))["status"] == "skipped"
        assert _fresh(world.db, world.range).state == m.RangeState.destroyed
        assert _fresh(world.db, snap).snapshot_state == "ready"

    def test_delete_snapshot_marks_the_row_deleted(self, world, snap, monkeypatch):
        snap.snapshot_state, snap.snapshot_data = "ready", '{"snapshot_name": "tnabc"}'
        world.db.commit()
        backend = _fake_backend(delete_snapshot=SnapshotDeleteResult(status="ok"))
        monkeypatch.setattr(tasks, "_get_backend", lambda name=None: backend)

        assert tasks.delete_snapshot(str(world.range.id), str(snap.id))["status"] == "deleted"
        assert backend.delete_snapshot.await_args.args[2] == "tnabc"
        assert _fresh(world.db, snap).snapshot_state == "deleted"


# -- AI orchestrator stand-in -------------------------------------------------------------
class _FakeHttpx:
    """``httpx.Client`` replacement: records posted JSON, answers with ``reply``."""

    def __init__(self, reply: dict):
        self.reply, self.posts = reply, []

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, **_):
        self.posts.append((url, json))
        return MagicMock(status_code=200, json=MagicMock(return_value=self.reply), raise_for_status=MagicMock())


# -- competencies, assertions, auto-assessments, learning recommendations ----------------
def _competency(code, category):
    return m.Competency(code=code, name=f"name {code}", framework=m.CompetencyFramework.nice, category=category)


class TestCompetencies:
    def test_auto_assess_writes_assessment_and_assertions(self, world):
        db, ex, user = world.db, world.exercise, world.user
        ex.total_score, ex.max_score = 30, 100
        db.commit()
        # Scenario yaml names T1566 (-> "Operate & Maintain") and T1059 (-> "Protect & Defend").
        k1, k2 = _add(db, _competency("K0001", "Protect & Defend"), _competency("K0002", "Operate & Maintain"))
        _add(
            db,
            _objective(ex, "o1", 30, achieved=True, competency_code="K0002"),
            _objective(ex, "o2", 70, competency_code="NO-SUCH-CODE"),
            _objective(ex, "o3", 10, competency_code=""),
        )

        out = tasks.auto_assess_competency(str(ex.id), str(user.id))

        assert out["status"] == "assessed" and out["mappings_count"] == 3
        a = db.scalars(select(m.CompetencyAutoAssessment)).one()
        assert str(a.id) == out["assessment_id"]
        assert (a.user_id, a.exercise_id, a.raw_score, a.max_score) == (user.id, ex.id, 30, 100)
        mappings = json.loads(a.competency_mappings)
        # Coded objectives first (unordered, as the query has no ORDER BY), then MITRE fallbacks.
        assert sorted(mp["competency_code"] for mp in mappings[:2]) == ["K0002", "NO-SUCH-CODE"]
        assert mappings[2]["competency_code"] == "K0001" and mappings[2]["technique"] == "T1059"
        by_code = {mp["competency_code"]: mp for mp in mappings}
        assert by_code["NO-SUCH-CODE"]["competency_id"] == ""  # recorded, not asserted

        rows = {r.competency_id: r for r in db.scalars(select(m.CompetencyAssertion)).all()}
        assert set(rows) == {k1.id, k2.id}  # no assertion row with an empty competency id
        # K0002: inserted from its objective (100% -> expert), then updated by the
        # exercise-wide score (30% -> novice) with the exercise appended as evidence.
        assert rows[k2.id].proficiency == m.ProficiencyLevel.novice
        assert json.loads(rows[k2.id].evidence_refs) == [str(ex.id), str(ex.id)]
        assert rows[k1.id].proficiency == m.ProficiencyLevel.novice
        assert rows[k1.id].source == "truenorth" and rows[k1.id].assessed_at is not None

    def test_auto_assess_for_a_missing_exercise_is_skipped(self, world):
        out = tasks.auto_assess_competency(str(uuid.uuid4()), str(world.user.id))
        assert out == {"status": "skipped", "reason": "exercise_not_found"}

    def test_evidence_keeps_the_last_ten(self, world):
        k1 = _add(world.db, _competency("K0001", "Analyze"))
        with tasks._db_session() as db:
            for i in range(12):
                tasks._upsert_competency_assertion(db, str(world.user.id), str(k1.id), 95, f"ex-{i}")
        row = world.db.scalars(select(m.CompetencyAssertion)).one()
        assert json.loads(row.evidence_refs) == [f"ex-{i}" for i in range(2, 12)]
        assert row.proficiency == m.ProficiencyLevel.expert

    def test_learning_recommendation_reads_profile_history_courses_and_stores(self, world, monkeypatch):
        db, user = world.db, world.user
        k1 = _add(db, _competency("K0001", "Analyze"))
        _add(db, m.CompetencyAssertion(user_id=user.id, competency_id=k1.id,
                                       proficiency=m.ProficiencyLevel.advanced))
        other_tenant = _add(db, m.Tenant(name="t2", slug="t2"))
        done = datetime(2026, 9, 1, tzinfo=UTC)
        _add(
            db,
            m.Exercise(name="done", range_id=world.range.id, tenant_id=world.tenant.id,
                       state=m.ExerciseState.completed, total_score=8, max_score=10, completed_at=done),
            m.Exercise(name="elsewhere", range_id=world.range.id, tenant_id=other_tenant.id,
                       state=m.ExerciseState.completed, completed_at=done),
            m.Course(name="Published", is_published=True, nice_work_roles='["PR-CDA-001"]'),
            m.Course(name="Draft", is_published=False),
        )
        fake = _FakeHttpx({"output": '```json\n{"summary": "ok", "gaps": ["x"], '
                                     '"target_role_readiness": 0.4}\n```', "model_used": "m1"})
        monkeypatch.setattr("httpx.Client", fake)

        out = tasks.generate_learning_recommendation(str(user.id), "analyst")

        payload = fake.posts[0][1]
        assert payload["user_profile"]["assertions"] == [
            {"code": "K0001", "name": "name K0001", "category": "Analyze", "proficiency": "advanced"}
        ]
        assert [e["name"] for e in payload["exercise_history"]] == ["done"]  # own tenant, completed only
        assert payload["exercise_history"][0]["score"] == 8
        assert [c["name"] for c in payload["available_courses"]] == ["Published"]
        rec = db.scalars(select(m.LearningRecommendation)).one()
        assert str(rec.id) == out["recommendation_id"]
        assert (rec.user_id, rec.summary, rec.target_role, rec.target_role_readiness) == (
            user.id, "ok", "analyst", 0.4
        )
        assert json.loads(rec.gaps) == ["x"] and json.loads(rec.strengths) == []
        assert rec.model_used == "m1" and rec.generated_at is not None


# -- forge: scenarios, exercises, forged_exercises -----------------------------------------
FORGED_YAML = "name: forged-op\ntimeline:\n  - t: '0:01'\n    action: T1059.001 powershell\n"


class TestForge:
    def test_forge_creates_scenario_exercise_and_tracking_row(self, world, monkeypatch):
        # Production defect: the replaced inserts left scenarios.is_public and
        # exercises.total_score/max_score (NOT NULL, no server default) empty, so every
        # forge failed on a real database after the AI call had been paid for.
        monkeypatch.setattr("httpx.Client", _FakeHttpx({"output": f"```yaml\n{FORGED_YAML}```", "model_used": "m1"}))
        tid = str(world.tenant.id)

        out = tasks.forge_exercise("req-12345678", tid, [{"id": "ioc-1"}, {"value": "no id"}],
                                   {"difficulty": "advanced", "feed_id": str(uuid.uuid4())})

        db = world.db
        scen = db.get(m.Scenario, uuid.UUID(out["scenario_id"]))
        assert (scen.name, scen.version, scen.is_public, scen.tenant_id) == ("forged-op", "1.0", False, world.tenant.id)
        assert scen.yaml == FORGED_YAML.strip()
        ex = db.get(m.Exercise, uuid.UUID(out["exercise_id"]))
        assert (ex.kind, ex.state, ex.total_score, ex.max_score) == ("assessment", m.ExerciseState.pending, 0, 0)
        assert (ex.range_id, ex.scenario_id) == (world.range.id, scen.id)
        fe = db.scalars(select(m.ForgedExercise)).one()
        assert (fe.exercise_id, fe.scenario_id, fe.difficulty, fe.model_used) == (ex.id, scen.id, "advanced", "m1")
        assert json.loads(fe.indicator_ids) == ["ioc-1"]
        assert json.loads(fe.mitre_techniques) == ["T1059.001"]

    def test_forge_without_a_range_rolls_back_the_scenario(self, world, notify, monkeypatch):
        monkeypatch.setattr("httpx.Client", _FakeHttpx({"output": FORGED_YAML}))
        empty = _add(world.db, m.Tenant(name="t3", slug="t3"))
        before = len(world.db.scalars(select(m.Scenario)).all())
        with pytest.raises(ValueError, match="No range"):
            tasks.forge_exercise("req-2", str(empty.id), [], {})
        assert len(world.db.scalars(select(m.Scenario)).all()) == before
        assert notify.call_args.args[1]["status"] == "failed"


# -- Postgres rendering ------------------------------------------------------------------
class _PgRecorder:
    """A session stand-in that compiles each statement for Postgres (psycopg) instead of running it."""

    def __init__(self):
        from sqlalchemy.dialects.postgresql import psycopg

        self.dialect = psycopg.dialect()
        self.sql: list[str] = []

    def get_bind(self):
        return SimpleNamespace(dialect=self.dialect)

    def execute(self, stmt, *_):
        self.sql.append(str(stmt.compile(dialect=self.dialect)))
        return MagicMock(first=MagicMock(return_value=None), fetchall=MagicMock(return_value=[]), rowcount=1,
                         scalar_one=MagicMock(return_value=uuid.uuid4()))


ID = str(uuid.uuid4())
PG_CALLS = {
    "update_range_state": lambda db: db_ops.update_range_state(
        db, ID, "failed", error="e", output="o", only_from=("ready", "stopped")
    ),
    "range_template_and_backend": lambda db: db_ops.range_template_and_backend(db, ID),
    "range_output_and_backend": lambda db: db_ops.range_output_and_backend(db, ID),
    "active_ranges": db_ops.active_ranges,
    "touch_range": lambda db: db_ops.touch_range(db, ID),
    "first_range_for_tenant": lambda db: db_ops.first_range_for_tenant(db, ID),
    "hypervisor_connection": lambda db: db_ops.hypervisor_connection(db, "vsphere"),
    "enabled_golden_images": lambda db: db_ops.enabled_golden_images(db, "vsphere"),
    "exercise_scenario_yaml": lambda db: db_ops.exercise_scenario_yaml(db, ID),
    "exercise_scores_and_yaml": lambda db: db_ops.exercise_scores_and_yaml(db, ID),
    "start_exercise": lambda db: db_ops.start_exercise(db, ID),
    "achieve_objective": lambda db: db_ops.achieve_objective(db, ID, "o1"),
    "running_exercises": lambda db: db_ops.running_exercises(db),
    "complete_exercise": lambda db: db_ops.complete_exercise(db, ID),
    "cancel_exercise": lambda db: db_ops.cancel_exercise(db, ID),
    "exercise_for_aar": lambda db: db_ops.exercise_for_aar(db, ID),
    "objectives_for_aar": lambda db: db_ops.objectives_for_aar(db, ID),
    "recent_completed_exercises": lambda db: db_ops.recent_completed_exercises(db, ID),
    "upsert_aar": lambda db: db_ops.upsert_aar(db, ID, "{}"),
    "update_snapshot_state": lambda db: db_ops.update_snapshot_state(
        db, ID, "ready", data="{}", size=1, only_from=("creating",)
    ),
    "snapshot_context": lambda db: db_ops.snapshot_context(db, ID, ID),
    "insert_forged_scenario": lambda db: db_ops.insert_forged_scenario(db, "n", "y", ID),
    "insert_forged_exercise": lambda db: db_ops.insert_forged_exercise(db, "n", ID, ID, ID),
    "insert_forged_exercise_record": lambda db: db_ops.insert_forged_exercise_record(
        db, exercise_id=ID, scenario_id=ID, feed_id=None, indicator_ids=[], scenario_yaml="y",
        mitre_techniques=[], difficulty="d", model_used="m", tenant_id=ID,
    ),
    "objective_competency_rows": lambda db: db_ops.objective_competency_rows(db, ID),
    "nice_competency_in_category": lambda db: db_ops.nice_competency_in_category(db, "Analyze"),
    "insert_auto_assessment": lambda db: db_ops.insert_auto_assessment(db, ID, ID, [], 1, 2),
    "upsert_competency_assertion": lambda db: db_ops.upsert_competency_assertion(db, ID, ID, "novice", ID),
    "competency_profile": lambda db: db_ops.competency_profile(db, ID),
    "published_courses": db_ops.published_courses,
    "insert_learning_recommendation": lambda db: db_ops.insert_learning_recommendation(db, ID, {}, "", "m"),
}


class TestPostgresRendering:
    @pytest.mark.parametrize("name", sorted(PG_CALLS))
    def test_every_helper_renders_for_postgres(self, name):
        db = _PgRecorder()
        PG_CALLS[name](db)
        assert db.sql, f"{name} executed nothing"
        for sql in db.sql:
            assert "now()" in sql.lower() or sql.lstrip().upper().startswith("SELECT"), sql

    def test_every_public_helper_is_covered(self):
        public = {n for n, f in vars(db_ops).items() if callable(f) and getattr(f, "__module__", "") == db_ops.__name__
                  and not n.startswith("_") and n != "aar_upsert_statement"}
        assert public == set(PG_CALLS)

    def test_aar_insert_is_on_conflict_do_nothing_returning_on_postgres(self):
        from sqlalchemy.dialects import postgresql

        stmt = db_ops.aar_upsert_statement(postgresql.insert, ID, "{}", "<p/>")
        sql = " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())
        assert sql.startswith("INSERT INTO after_action_reports (id, exercise_id, report_json, report_html,")
        assert "ON CONFLICT (exercise_id) DO NOTHING" in sql
        assert sql.endswith("RETURNING after_action_reports.id")

    def test_range_state_binds_like_the_api_model_and_casts_the_guard(self):
        import sqlalchemy as sa

        db = _PgRecorder()
        db_ops.update_range_state(db, ID, "failed", only_from=("ready",))
        sql = " ".join(db.sql[0].split())
        # The native enum is bound exactly as the API's ORM binds it through the same dialect.
        api = str(sa.update(m.Range.__table__).values(state="failed").compile(dialect=db.dialect))
        assert api.startswith("UPDATE ranges SET state=%(state)s") and "state=%(state)s," in sql
        assert "CAST(ranges.state AS TEXT) IN" in sql
        assert "ranges.id = %(id_1)s::UUID" in sql
