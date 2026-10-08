"""Pause and resume an exercise's scenario run.

Until 2026-10-08 a paused exercise could not be resumed: there was no endpoint, and the
run task stopped for good at its next event. ``POST /exercises/{id}/resume`` now moves a
paused exercise back to running and dispatches the run again with a new lease
(``exercise_runs``); the new task continues the same run after the last event the old
one claimed, so every inject fires once. These tests drive the API and then run the
worker task it dispatched, on the same database.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
import yaml

pytest.importorskip("celery")

from app.models import Exercise, ExerciseState, Range, Scenario, Template  # noqa: E402
from app.scenario_runs import ExerciseRun, InjectRecord  # noqa: E402

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
TIMELINE = [
    {"t": "0:00", "action": "simulated_execution", "params": {"technique": "T1059", "target": "ws-01"}},
    {"t": "0:30", "action": "simulated_execution", "params": {"technique": "T1003", "target": "ws-01"}},
    {"t": "1:00", "action": "network_scan", "params": {"scan_type": "port_scan", "target_network": "10.0.1.0/24"}},
]
RUN_TASK = "worker.tasks.run_scenario_v2"


def _exercise(db, state=ExerciseState.pending, tenant=DEV_TENANT) -> Exercise:
    tpl = Template(name=f"t-{uuid.uuid4().hex[:6]}", version="1.0", yaml="name: t\nnodes: []\n", tenant_id=tenant)
    db.add(tpl)
    db.flush()
    rng = Range(name="r", template_id=tpl.id, tenant_id=tenant, state="ready", provisioner_backend="mock")
    sc = Scenario(name="s", yaml=yaml.safe_dump({"name": "s", "timeline": TIMELINE}), tenant_id=tenant)
    db.add_all([rng, sc])
    db.flush()
    ex = Exercise(name="ex", range_id=rng.id, scenario_id=sc.id, tenant_id=tenant, state=state)
    db.add(ex)
    db.commit()
    return ex


def _run_sends(broker) -> list[list]:
    return [args for name, args in broker.sent if name == RUN_TASK]


@pytest.fixture
def worker(db_session, monkeypatch):
    """The worker's run loop, on the test's database; returns the events it dispatched."""
    from worker import exercise_run, inject_dispatch, tasks

    @contextmanager
    def _session():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(tasks, "_db_session", _session)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    monkeypatch.setattr(exercise_run.time, "sleep", lambda s: None)
    fired: list[int] = []
    real = inject_dispatch.dispatch_inject
    hooks: dict[int, object] = {}

    def _dispatch(exercise_id, action, params, *, seq=None, **kw):
        if seq in hooks:
            hooks.pop(seq)()
        fired.append(seq)
        return real(exercise_id, action, params, seq=seq, **kw)

    monkeypatch.setattr(inject_dispatch, "dispatch_inject", _dispatch)

    def run(args):
        return exercise_run.run_scenario_v2(*args)

    run.fired, run.hooks = fired, hooks
    return run


def _timeline_records(db, ex) -> list[tuple[int, str]]:
    db.expire_all()
    rows = db.query(InjectRecord).filter(InjectRecord.exercise_id == ex.id, InjectRecord.source == "timeline")
    return sorted((r.seq, r.run_id) for r in rows)


def _state(db, ex) -> ExerciseState:
    db.expire_all()
    return db.get(Exercise, ex.id).state


class TestPauseResumeRun:
    def test_pause_then_resume_fires_the_remaining_injects_once(self, client, db_session, no_real_broker, worker):
        ex = _exercise(db_session)
        assert client.post(f"/exercises/{ex.id}/start").status_code == 200
        [first] = _run_sends(no_real_broker)
        assert len(first) == 3 and first[2]  # exercise id, definition, lease

        # The instructor pauses once the first inject has fired.
        worker.hooks[1] = lambda: client.post(f"/exercises/{ex.id}/pause")
        out = worker(first)
        assert out["status"] == "halted" and out["state"] == "paused"
        assert worker.fired == [0, 1]  # event 1 was claimed before the pause
        assert _state(db_session, ex) == ExerciseState.paused

        resp = client.post(f"/exercises/{ex.id}/resume")
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "running"
        [_, second] = _run_sends(no_real_broker)
        assert second[2] != first[2]  # a new lease

        out = worker(second)
        assert out["status"] == "completed"
        assert worker.fired == [0, 1, 2]  # each event exactly once, in order
        records = _timeline_records(db_session, ex)
        assert [seq for seq, _ in records] == [0, 1, 2]
        assert len({run for _, run in records}) == 1  # one run, continued
        assert _state(db_session, ex) == ExerciseState.completed

    def test_a_pre_pause_task_still_alive_cannot_fire_after_the_resume(self, client, db_session, no_real_broker, worker):
        """Pause and resume land while the first task is mid-event: it has claimed event 1
        but not recorded it. The old task must stop, and the new one must not re-fire 1."""
        ex = _exercise(db_session)
        client.post(f"/exercises/{ex.id}/start")
        [first] = _run_sends(no_real_broker)

        def _pause_and_resume():
            assert client.post(f"/exercises/{ex.id}/pause").status_code == 200
            assert client.post(f"/exercises/{ex.id}/resume").status_code == 200

        worker.hooks[1] = _pause_and_resume
        out = worker(first)
        assert out["status"] == "halted" and out["state"] == "superseded"
        assert worker.fired == [0, 1]

        [_, second] = _run_sends(no_real_broker)
        out = worker(second)
        assert out["status"] == "completed"
        assert worker.fired == [0, 1, 2]
        assert [seq for seq, _ in _timeline_records(db_session, ex)] == [0, 1, 2]

    def test_a_late_redelivery_of_the_first_task_does_nothing(self, client, db_session, no_real_broker, worker):
        ex = _exercise(db_session)
        client.post(f"/exercises/{ex.id}/start")
        [first] = _run_sends(no_real_broker)
        worker.hooks[1] = lambda: client.post(f"/exercises/{ex.id}/pause")
        worker(first)
        client.post(f"/exercises/{ex.id}/resume")
        out = worker(first)  # the broker delivers the old message again
        assert out["status"] == "skipped" and out.get("superseded") is True
        assert worker.fired == [0, 1]

    def test_resume_before_any_event_fired_starts_from_the_first(self, client, db_session, no_real_broker, worker):
        ex = _exercise(db_session)
        client.post(f"/exercises/{ex.id}/start")
        client.post(f"/exercises/{ex.id}/pause")
        client.post(f"/exercises/{ex.id}/resume")
        [first, second] = _run_sends(no_real_broker)
        assert worker(first)["status"] == "skipped"  # superseded before it ran
        assert worker(second)["status"] == "completed"
        assert worker.fired == [0, 1, 2]

    def test_resume_records_the_lease_swap_and_cursor(self, client, db_session, no_real_broker, worker):
        ex = _exercise(db_session)
        client.post(f"/exercises/{ex.id}/start")
        db_session.expire_all()
        before = db_session.get(ExerciseRun, ex.id)
        run_id, lease = before.run_id, before.lease
        worker.hooks[2] = lambda: client.post(f"/exercises/{ex.id}/pause")
        worker(_run_sends(no_real_broker)[0])
        client.post(f"/exercises/{ex.id}/resume")
        db_session.expire_all()
        after = db_session.get(ExerciseRun, ex.id)
        assert (after.run_id, after.next_seq, after.resume_from) == (run_id, 3, 3)
        assert after.lease != lease

    def test_replay_after_completion_is_a_new_run(self, client, db_session, no_real_broker, worker):
        ex = _exercise(db_session)
        client.post(f"/exercises/{ex.id}/start")
        worker(_run_sends(no_real_broker)[0])
        assert _state(db_session, ex) == ExerciseState.completed
        assert client.post(f"/exercises/{ex.id}/run").status_code == 409  # replay needs reset=true (sweep M3)
        assert client.post(f"/exercises/{ex.id}/run", params={"reset": "true"}).status_code == 200
        worker(_run_sends(no_real_broker)[1])
        records = _timeline_records(db_session, ex)
        assert [seq for seq, _ in records] == [0, 0, 1, 1, 2, 2]
        assert len({run for _, run in records}) == 2


class TestResumeIsRefused:
    @pytest.mark.parametrize(
        "state", [ExerciseState.completed, ExerciseState.cancelled, ExerciseState.pending, ExerciseState.running]
    )
    def test_only_a_paused_exercise_can_be_resumed(self, client, db_session, no_real_broker, state):
        ex = _exercise(db_session, state=state)
        resp = client.post(f"/exercises/{ex.id}/resume")
        assert resp.status_code == 409
        assert resp.json()["detail"] == f"Exercise is {state.value}, expected paused"
        assert _run_sends(no_real_broker) == []
        assert _state(db_session, ex) == state

    def test_resume_of_a_completed_exercise_after_a_real_run_is_refused(
        self, client, db_session, no_real_broker, worker
    ):
        ex = _exercise(db_session)
        client.post(f"/exercises/{ex.id}/start")
        worker.hooks[1] = lambda: client.post(f"/exercises/{ex.id}/pause")
        worker(_run_sends(no_real_broker)[0])
        assert client.post(f"/exercises/{ex.id}/complete").status_code == 200
        assert client.post(f"/exercises/{ex.id}/resume").status_code == 409
        assert len(_run_sends(no_real_broker)) == 1
        assert _state(db_session, ex) == ExerciseState.completed

    def test_another_tenants_exercise_is_404(self, client, db_session, no_real_broker):
        from app.models import Tenant

        other = Tenant(name="other", slug=f"other-{uuid.uuid4().hex[:6]}")
        db_session.add(other)
        db_session.flush()
        ex = _exercise(db_session, state=ExerciseState.paused, tenant=other.id)
        assert client.post(f"/exercises/{ex.id}/resume").status_code == 404
        assert _run_sends(no_real_broker) == []
