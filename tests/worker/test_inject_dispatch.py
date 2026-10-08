"""Inject dispatch and the scenario run loop, on a real (SQLite) worker database.

Every test here observes recorded outcomes (``inject_records`` rows, exercise state, the
telemetry task the dispatch sent), not mocks of the seam. Injectors are the real
scenario-engine registry; all of them are simulated, so nothing touches a host.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

pytest.importorskip("celery")

from scenario_engine import injectors as engine  # noqa: E402
from worker import db_ops, exercise_run, inject_dispatch, tables  # noqa: E402

INGEST = "worker.tasks.ingest_telemetry_batch"
MIRRORED = (
    tables.exercises,
    tables.objectives,
    tables.scenarios,
    tables.ranges,
    tables.scenario_executions,
    tables.inject_records,
)


def _sqlite_now(dbapi_conn, _record):
    if type(dbapi_conn).__module__.startswith("sqlite3"):
        dbapi_conn.create_function("now", 0, lambda: datetime.now(UTC).isoformat())


@pytest.fixture
def wdb(tmp_path, monkeypatch):
    """The worker's scenario tables in a file SQLite DB, tasks pointed at it.

    Built from the generated mirror (worker/tables.py) with NOT NULL relaxed, so a test
    seeds only the columns the run loop reads; the real schema runs in
    test_worker_sql_real_db.py."""
    from worker import tasks

    sa.event.listen(sa.engine.Engine, "connect", _sqlite_now)
    url = f"sqlite:///{tmp_path / 'scenario.db'}"
    eng = sa.create_engine(url)
    _relaxed_mirror().create_all(eng)
    monkeypatch.setattr(tasks, "DATABASE_URL", url)
    notes: list[tuple[str, dict]] = []
    monkeypatch.setattr(tasks, "_notify_api", lambda ch, msg: notes.append((ch, msg)))
    monkeypatch.setattr(exercise_run.time, "sleep", lambda s: None)
    eng.notes = notes
    yield eng
    sa.event.remove(sa.engine.Engine, "connect", _sqlite_now)
    eng.dispose()


def _relaxed_mirror() -> sa.MetaData:
    md = sa.MetaData()
    for t in MIRRORED:
        sa.Table(t.name, md, *[sa.Column(c.name, c.type, primary_key=c.primary_key) for c in t.columns])
    return md


def _range(eng, *, backend="mock", vms=1, output=True) -> str:
    rid = str(uuid.uuid4())
    out = None
    if output:
        out = json.dumps(
            {
                "vms": [{"name": f"vm-{i}", "ip": f"10.0.1.{10 + i}"} for i in range(vms)],
                "networks": [{"name": "net-0", "cidr": "10.0.1.0/24"}],
            }
        )
    with eng.begin() as c:
        c.execute(
            sa.insert(tables.ranges).values(
                id=rid, tenant_id=str(uuid.uuid4()), provisioner_backend=backend, provisioner_output=out
            )
        )
    return rid


def _exercise(eng, range_id: str | None, state="running", objectives=()) -> str:
    eid = str(uuid.uuid4())
    with eng.begin() as c:
        c.execute(
            sa.insert(tables.exercises).values(
                id=eid, range_id=range_id, tenant_id=str(uuid.uuid4()), state=state, total_score=0, max_score=0
            )
        )
        for ref, pts in objectives:
            c.execute(
                sa.insert(tables.objectives).values(
                    id=str(uuid.uuid4()), exercise_id=eid, ref_id=ref, points=pts, achieved=False
                )
            )
    return eid


def _records(eng, **where) -> list[dict]:
    r = tables.inject_records
    stmt = sa.select(r).order_by(r.c.seq)
    for k, v in where.items():
        stmt = stmt.where(r.c[k] == v)
    with eng.connect() as c:
        return [dict(row._mapping) for row in c.execute(stmt)]


def _state(eng, eid: str) -> str:
    with eng.connect() as c:
        return c.execute(sa.select(tables.exercises.c.state).where(tables.exercises.c.id == eid)).scalar()


def _ingest_sends(broker) -> list[list]:
    return [args for name, args in broker.sent if name == INGEST]


# -- dispatch_inject ------------------------------------------------------------
class TestDispatchInject:
    def test_mock_backend_runs_a_simulated_injector_and_ships_its_telemetry(self, wdb, no_real_broker):
        rid = _range(wdb)
        eid = _exercise(wdb, rid)
        out = inject_dispatch.dispatch_inject(eid, "simulated_execution", {"technique": "T1059"}, seq=0, t="0:00")

        assert out["status"] == "fired" and out["dispatched"] is True
        assert out["execution_mode"] == "simulated"
        assert out["mitre_technique"] == "T1059"  # params reach the injector
        [rec] = _records(wdb, exercise_id=eid)
        assert (rec["status"], rec["action"], rec["seq"], rec["telemetry_count"]) == (
            "fired",
            "simulated_execution",
            0,
            1,
        )
        assert rec["telemetry_shipped"] is True
        [[range_arg, events]] = _ingest_sends(no_real_broker)
        assert range_arg == rid
        labels = events[0]["tn_ground_truth"]  # labels kept apart from observables (sweep H4)
        assert labels["exercise_id"] == eid and labels["inject_action"] == "simulated_execution"
        assert labels["simulated"] is True  # synthetic, never mistaken for host activity
        assert "exercise_id" not in events[0] and "inject_action" not in events[0]
        assert (
            "exercise",
            {"id": eid, "event": "inject", "action": "simulated_execution", "status": "fired", "seq": 0, "t": "0:00"},
        ) in wdb.notes

    def test_mock_backend_skips_an_injector_that_needs_range_hosts(self, wdb, no_real_broker):
        eid = _exercise(wdb, _range(wdb))
        out = inject_dispatch.dispatch_inject(eid, "dns_spike", {"domains": ["a.test"], "count": 3})

        assert out["status"] == "skipped" and out["dispatched"] is False
        assert out["detail"] == "skipped: mock backend (injector needs range hosts)"
        assert [r["status"] for r in _records(wdb, exercise_id=eid)] == ["skipped"]
        assert _ingest_sends(no_real_broker) == []  # nothing ran, so no telemetry

    def test_live_backend_runs_host_injectors_when_the_range_has_hosts(self, wdb, no_real_broker):
        eid = _exercise(wdb, _range(wdb, backend="vsphere_api", vms=2))
        out = inject_dispatch.dispatch_inject(eid, "dns_spike", {"domains": ["a.test"], "count": 3})
        assert out["status"] == "fired" and out["backend"] == "vsphere_api"
        assert out["execution_mode"] == "simulated"  # honest: today's injector is synthetic

    def test_live_backend_without_provisioned_hosts_skips_host_injectors(self, wdb):
        eid = _exercise(wdb, _range(wdb, backend="vsphere_api", output=False))
        out = inject_dispatch.dispatch_inject(eid, "dns_spike", {"domains": ["a.test"], "count": 3})
        assert out["status"] == "skipped"
        assert out["detail"] == "skipped: range has no provisioned hosts"

    def test_unknown_action_is_recorded_failed(self, wdb, no_real_broker):
        eid = _exercise(wdb, _range(wdb))
        out = inject_dispatch.dispatch_inject(eid, "deploy_malware", {})
        assert out["status"] == "failed" and out["dispatched"] is False
        assert "No injector registered for action 'deploy_malware'" in out["detail"]
        assert [r["status"] for r in _records(wdb, exercise_id=eid)] == ["failed"]
        assert _ingest_sends(no_real_broker) == []

    def test_invalid_params_are_recorded_failed(self, wdb):
        eid = _exercise(wdb, _range(wdb))
        out = inject_dispatch.dispatch_inject(eid, "c2_beacon", {"c2_type": "smoke_signals", "target_host": "ws"})
        assert out["status"] == "failed"
        assert out["detail"].startswith("Invalid params")

    def test_injector_exception_is_recorded_failed_not_raised(self, wdb, monkeypatch):
        class Boom(engine.BaseInjector):
            name = "boom_test"
            touches_range_hosts = False

            def execute(self, context):
                raise RuntimeError("kaboom")

        monkeypatch.setitem(engine._INJECTOR_REGISTRY, "boom_test", Boom)
        eid = _exercise(wdb, _range(wdb))
        out = inject_dispatch.dispatch_inject(eid, "boom_test", {})
        assert out["status"] == "failed" and out["detail"] == "Injector error: kaboom"
        assert _records(wdb, exercise_id=eid)[0]["detail"] == "Injector error: kaboom"

    def test_missing_range_is_recorded_failed(self, wdb):
        eid = _exercise(wdb, str(uuid.uuid4()))  # range row does not exist
        out = inject_dispatch.dispatch_inject(eid, "simulated_execution", {})
        assert out["status"] == "failed" and "not found" in out["detail"]
        assert [r["status"] for r in _records(wdb, exercise_id=eid)] == ["failed"]

    def test_missing_exercise_is_reported_and_not_recorded(self, wdb):
        out = inject_dispatch.dispatch_inject(str(uuid.uuid4()), "simulated_execution", {})
        assert out == {
            "dispatched": False,
            "action": "simulated_execution",
            "status": "failed",
            "detail": "exercise not found",
            "exercise_id": out["exercise_id"],
            "skipped": False,
        }
        assert _records(wdb) == []

    def test_scenario_engine_unavailable_is_recorded_failed(self, wdb, monkeypatch):
        def _gone():
            raise inject_dispatch.EngineUnavailableError("scenario-engine not importable (test)")

        monkeypatch.setattr(inject_dispatch, "_engine", _gone)
        eid = _exercise(wdb, _range(wdb))
        out = inject_dispatch.dispatch_inject(eid, "simulated_execution", {})
        assert out["status"] == "failed" and "scenario-engine not importable" in out["detail"]

    def test_a_refused_telemetry_send_is_recorded_not_raised(self, wdb, monkeypatch):
        from celery import Celery

        def _refuse(*a, **k):
            raise ConnectionError("broker down")

        # On the class, as conftest's no_real_broker does: an instance attribute would
        # outlive monkeypatch's undo and leak into later tests.
        monkeypatch.setattr(Celery, "send_task", _refuse)
        eid = _exercise(wdb, _range(wdb))
        out = inject_dispatch.dispatch_inject(eid, "simulated_execution", {})
        assert out["status"] == "fired" and out["telemetry_shipped"] is False
        assert _records(wdb, exercise_id=eid)[0]["telemetry_shipped"] is False


# -- run_scenario_v2 ------------------------------------------------------------
TIMELINE = [
    {"t": "0:00", "action": "simulated_execution", "params": {"technique": "T1059", "target": "ws-01"}},
    {"t": "0:30", "action": "dns_spike", "params": {"domains": ["evil.test"], "count": 5}},
    {"t": "1:00", "action": "network_scan", "params": {"scan_type": "port_scan", "target_network": "10.0.1.0/24"}},
]


class TestRunScenarioV2:
    def test_mock_run_fires_safe_injects_records_every_event_and_completes(self, wdb, monkeypatch, no_real_broker):
        monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
        eid = _exercise(wdb, _range(wdb), state="pending", objectives=[("obj-1", 10), ("obj-2", 5)])
        definition = {"timeline": TIMELINE, "objectives": [{"ref_id": "obj-1"}, {"ref_id": "obj-2"}]}

        result = exercise_run.run_scenario_v2(exercise_id=eid, scenario_definition=definition)

        assert result["status"] == "completed"
        assert (result["events_executed"], result["injects_fired"], result["objectives_completed"]) == (3, 2, 2)
        recs = _records(wdb, exercise_id=eid)
        assert [(r["seq"], r["action"], r["status"]) for r in recs] == [
            (0, "simulated_execution", "fired"),
            (1, "dns_spike", "skipped"),
            (2, "network_scan", "fired"),
        ]
        assert len({r["run_id"] for r in recs}) == 1
        assert len(_ingest_sends(no_real_broker)) == 2
        assert _state(wdb, eid) == "completed"
        with wdb.connect() as c:
            total = c.execute(
                sa.select(tables.exercises.c.total_score).where(tables.exercises.c.id == eid)
            ).scalar()
        assert total == 15

    @pytest.mark.parametrize("terminal", ["completed", "cancelled", "paused"])
    def test_a_late_delivery_does_not_touch_a_newer_state(self, wdb, terminal):
        eid = _exercise(wdb, _range(wdb), state=terminal)
        result = exercise_run.run_scenario_v2(exercise_id=eid, scenario_definition={"timeline": TIMELINE})
        assert result == {"status": "skipped", "exercise_id": eid, "state": terminal}
        assert _state(wdb, eid) == terminal
        assert _records(wdb) == []

    def test_missing_exercise_is_skipped(self, wdb):
        result = exercise_run.run_scenario_v2(exercise_id=str(uuid.uuid4()), scenario_definition={"timeline": []})
        assert result["status"] == "skipped" and result["state"] is None

    def test_stops_firing_when_an_instructor_ends_the_exercise_mid_run(self, wdb, monkeypatch):
        eid = _exercise(wdb, _range(wdb))
        real = inject_dispatch.dispatch_inject

        def _then_cancel(*a, **k):
            out = real(*a, **k)
            with wdb.begin() as c:
                c.execute(
                    sa.update(tables.exercises).where(tables.exercises.c.id == eid).values(state="cancelled")
                )
            return out

        monkeypatch.setattr(inject_dispatch, "dispatch_inject", _then_cancel)
        result = exercise_run.run_scenario_v2(exercise_id=eid, scenario_definition={"timeline": TIMELINE})
        assert result["status"] == "halted" and result["state"] == "cancelled"
        assert len(_records(wdb, exercise_id=eid)) == 1  # nothing after the cancel
        assert _state(wdb, eid) == "cancelled"

    def test_completion_does_not_overwrite_a_state_set_during_the_last_event(self, wdb, monkeypatch):
        eid = _exercise(wdb, _range(wdb))
        real = inject_dispatch.dispatch_inject

        def _complete_after(*a, **k):
            out = real(*a, **k)
            with wdb.begin() as c:
                c.execute(
                    sa.update(tables.exercises).where(tables.exercises.c.id == eid).values(state="completed")
                )
            return out

        monkeypatch.setattr(inject_dispatch, "dispatch_inject", _complete_after)
        result = exercise_run.run_scenario_v2(exercise_id=eid, scenario_definition={"timeline": TIMELINE[:1]})
        assert result["status"] == "halted" and result["state"] == "completed"

    def test_failure_on_a_finished_exercise_does_not_cancel_it(self, wdb, monkeypatch):
        """The failure handler is guarded too: an exercise completed by an instructor while
        the run was failing stays completed."""
        eid = _exercise(wdb, _range(wdb))

        def _complete_then_raise(*a, **k):
            with wdb.begin() as c:
                c.execute(
                    sa.update(tables.exercises).where(tables.exercises.c.id == eid).values(state="completed")
                )
            raise RuntimeError("worker lost the range")

        monkeypatch.setattr(inject_dispatch, "dispatch_inject", _complete_then_raise)
        with pytest.raises(RuntimeError, match="worker lost the range"):
            exercise_run.run_scenario_v2(exercise_id=eid, scenario_definition={"timeline": TIMELINE})
        assert _state(wdb, eid) == "completed"

    def test_failure_on_the_last_attempt_cancels_a_running_exercise(self, wdb, monkeypatch):
        eid = _exercise(wdb, _range(wdb))
        monkeypatch.setattr(inject_dispatch, "dispatch_inject", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        with pytest.raises(OSError):
            exercise_run.run_scenario_v2(exercise_id=eid, scenario_definition={"timeline": TIMELINE})
        assert _state(wdb, eid) == "cancelled"
        assert wdb.notes[-1] == ("exercise", {"id": eid, "state": "failed", "error": "x"})

    def test_a_retry_does_not_fire_events_its_run_already_recorded(self, wdb, monkeypatch):
        eid = _exercise(wdb, _range(wdb))
        monkeypatch.setattr(exercise_run, "_run_id", lambda task: "run-1")
        with wdb.begin() as c:
            db_ops.record_inject(
                c, exercise_id=eid, run_id="run-1", seq=0, t="0:00", action="simulated_execution", status="fired"
            )
        result = exercise_run.run_scenario_v2(exercise_id=eid, scenario_definition={"timeline": TIMELINE})
        assert result["status"] == "completed" and result["events_executed"] == 3
        assert [r["seq"] for r in _records(wdb, exercise_id=eid)] == [0, 1, 2]  # seq 0 not fired twice

    def test_a_replay_is_a_new_run_and_keeps_earlier_evidence(self, wdb):
        eid = _exercise(wdb, _range(wdb))
        exercise_run.run_scenario_v2(exercise_id=eid, scenario_definition={"timeline": TIMELINE[:1]})
        with wdb.begin() as c:
            c.execute(sa.update(tables.exercises).where(tables.exercises.c.id == eid).values(state="pending"))
        exercise_run.run_scenario_v2(exercise_id=eid, scenario_definition={"timeline": TIMELINE[:1]})
        recs = _records(wdb, exercise_id=eid)
        assert len(recs) == 2 and len({r["run_id"] for r in recs}) == 2

    def test_malformed_events_are_recorded_failed_and_the_run_continues(self, wdb):
        eid = _exercise(wdb, _range(wdb))
        timeline = [
            "not-a-mapping",
            {"t": "0:10"},
            {"t": "soon", "action": "simulated_execution"},
            {"t": "0:20", "action": "simulated_execution", "params": ["x"]},
            {"t": "0:30", "action": "simulated_execution"},
        ]
        result = exercise_run.run_scenario_v2(exercise_id=eid, scenario_definition={"timeline": timeline})
        assert result["status"] == "completed" and result["injects_fired"] == 1
        recs = _records(wdb, exercise_id=eid)
        assert [r["status"] for r in recs] == ["failed", "failed", "failed", "failed", "fired"]
        assert recs[2]["detail"] == "invalid event: timeline offset 'soon' is not m:ss"


class TestRunScenarioV1:
    def test_v1_reads_the_scenario_yaml_and_dispatches_each_event(self, wdb):
        sid = str(uuid.uuid4())
        eid = _exercise(wdb, _range(wdb))
        doc = "timeline:\n  - {t: '0:00', action: simulated_execution}\n  - {t: '0:05', action: nope}\n"
        with wdb.begin() as c:
            c.execute(sa.insert(tables.scenarios).values(id=sid, yaml=doc))
            c.execute(sa.update(tables.exercises).where(tables.exercises.c.id == eid).values(scenario_id=sid))
        result = exercise_run.run_scenario(exercise_id=eid)
        assert result == {"status": "completed", "exercise_id": eid, "events_executed": 2}
        assert [r["status"] for r in _records(wdb, exercise_id=eid)] == ["fired", "failed"]

    def test_v1_missing_exercise(self, wdb):
        assert exercise_run.run_scenario(exercise_id=str(uuid.uuid4()))["status"] == "error"


# -- run_inject (instructor) ----------------------------------------------------
class TestRunInjectTask:
    def test_fires_into_a_running_exercise(self, wdb):
        eid = _exercise(wdb, _range(wdb))
        out = exercise_run.run_inject(eid, "simulated_execution", {"technique": "T1003"})
        assert out["status"] == "fired"
        [rec] = _records(wdb, exercise_id=eid)
        assert (rec["source"], rec["seq"]) == ("instructor", None)

    @pytest.mark.parametrize("state", ["pending", "paused", "completed", "cancelled"])
    def test_not_running_is_recorded_skipped(self, wdb, state):
        eid = _exercise(wdb, _range(wdb), state=state)
        out = exercise_run.run_inject(eid, "simulated_execution", {})
        assert out["status"] == "skipped" and out["dispatched"] is False
        assert [(r["status"], r["detail"]) for r in _records(wdb, exercise_id=eid)] == [
            ("skipped", f"skipped: exercise is {state}")
        ]

    def test_missing_exercise(self, wdb):
        out = exercise_run.run_inject(str(uuid.uuid4()), "simulated_execution")
        assert out["status"] == "failed" and out["detail"] == "exercise not found"


# -- run_scenario_execution -------------------------------------------------------
def _execution(eng, range_id, state="pending") -> str:
    xid = str(uuid.uuid4())
    with eng.begin() as c:
        c.execute(
            sa.insert(tables.scenario_executions).values(
                id=xid, tenant_id=str(uuid.uuid4()), range_id=range_id, state=state
            )
        )
    return xid


def _xstate(eng, xid):
    x = tables.scenario_executions
    with eng.connect() as c:
        return c.execute(sa.select(x.c.state, x.c.error).where(x.c.id == xid)).first()


class TestRunScenarioExecution:
    def test_runs_the_timeline_and_completes(self, wdb):
        xid = _execution(wdb, _range(wdb))
        out = exercise_run.run_scenario_execution(xid, {"timeline": TIMELINE + [{"t": "x"}]})
        assert out["status"] == "completed" and out["injects_fired"] == 2
        assert [r["status"] for r in _records(wdb, execution_id=xid)] == ["fired", "skipped", "fired", "failed"]
        assert _xstate(wdb, xid)[0] == "completed"

    def test_a_finished_execution_is_not_rerun(self, wdb):
        xid = _execution(wdb, _range(wdb), state="completed")
        assert exercise_run.run_scenario_execution(xid, {"timeline": TIMELINE})["status"] == "skipped"
        assert _records(wdb) == []

    def test_failure_marks_the_execution_failed(self, wdb, monkeypatch):
        xid = _execution(wdb, _range(wdb))

        def _boom(*a, **k):
            raise RuntimeError("db went away")

        monkeypatch.setattr(inject_dispatch, "dispatch_for_execution", _boom)
        with pytest.raises(RuntimeError):
            exercise_run.run_scenario_execution(xid, {"timeline": TIMELINE})
        assert tuple(_xstate(wdb, xid)) == ("failed", "db went away")

    def test_missing_range_records_failed_injects(self, wdb):
        xid = _execution(wdb, None)
        exercise_run.run_scenario_execution(xid, {"timeline": TIMELINE[:1]})
        [rec] = _records(wdb, execution_id=xid)
        assert (rec["status"], rec["detail"]) == ("failed", "no range to inject into")
