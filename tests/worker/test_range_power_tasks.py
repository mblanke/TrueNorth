"""stop_range / start_range act only for the operation the API recorded (worker/fencing.py).

Before: the API flipped the range to ``stopped``/``running`` itself and the task powered
VMs off or on whatever the range's state was by then, so a duplicate or late delivery
powered a range off again after it had been started, and the state said "stopped"
before (or whether) anything was powered off. Now the API records a stop as an operation
and moves the range to ``stopping`` (``starting`` for a start); the task claims the range
from that state, writes ``stopped``/``running`` only from it, and does nothing at all for
a range in any other state.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from app import models as m
from app.sections import Base
from sqlalchemy.orm import Session, sessionmaker

tasks = pytest.importorskip("worker.tasks")
from worker.provisioners.results import StartResult, StopResult  # noqa: E402

TASKS = {"stop": ("stop_range", "stopping", "stopped"), "start": ("start_range", "starting", "running")}


@pytest.fixture
def factory(tmp_path, monkeypatch):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'tn.db'}")
    Base.metadata.create_all(eng)
    make = sessionmaker(bind=eng, class_=Session, expire_on_commit=False)

    @contextmanager
    def _session():
        s = make()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(tasks, "_db_session", _session)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    yield make
    eng.dispose()


@pytest.fixture
def backend(monkeypatch):
    b = MagicMock()
    b.stop = AsyncMock(return_value=StopResult(status="ok", vms_stopped=2))
    b.start = AsyncMock(return_value=StartResult(status="ok", vms_started=2))
    monkeypatch.setattr(tasks, "_get_backend", lambda name=None: b)
    return b


def _range(make, state: str) -> str:
    with make() as s:
        t = m.Tenant(name=f"t-{uuid.uuid4().hex[:6]}", slug=f"t-{uuid.uuid4().hex[:6]}")
        s.add(t)
        s.flush()
        tmpl = m.Template(name="t", yaml="id: t\n", tenant_id=t.id)
        s.add(tmpl)
        s.flush()
        r = m.Range(name="r", template_id=tmpl.id, tenant_id=t.id, provisioner_backend="mock",
                    provisioner_output='{"vms": [{"vm_id": "vm-1"}]}', state=m.RangeState(state),
                    error_message="an earlier failure")
        s.add(r)
        s.commit()
        return str(r.id)


def _state(make, rid):
    with make() as s:
        r = s.get(m.Range, uuid.UUID(rid))
        return r.state.value, r.error_message


def _run(action, rid):
    return getattr(tasks, TASKS[action][0]).run(rid)


@pytest.mark.parametrize("action", ["stop", "start"])
def test_a_recorded_power_operation_powers_the_vms_and_reaches_its_state(factory, backend, action):
    _, in_progress, done = TASKS[action]
    rid = _range(factory, in_progress)
    assert _run(action, rid)["status"] == done
    getattr(backend, action).assert_awaited_once_with(rid, {"vms": [{"vm_id": "vm-1"}]})
    assert _state(factory, rid) == (done, None), "the observed state, written by the worker, error cleared"


@pytest.mark.parametrize("action", ["stop", "start"])
@pytest.mark.parametrize("state", ["ready", "running", "stopped", "destroying", "destroyed", "failed"])
def test_a_duplicate_or_stale_power_task_touches_nothing(factory, backend, action, state):
    rid = _range(factory, state)
    assert _run(action, rid)["status"] == "skipped"
    backend.stop.assert_not_called()
    backend.start.assert_not_called()
    assert _state(factory, rid)[0] == state


@pytest.mark.parametrize("action", ["stop", "start"])
def test_a_failed_attempt_that_will_be_retried_leaves_the_range_in_progress(factory, backend, action):
    _, in_progress, done = TASKS[action]
    rid = _range(factory, in_progress)
    getattr(backend, action).return_value = (StopResult if action == "stop" else StartResult)(
        status="failed", errors=["vm-1: host down"])
    with patch.object(tasks, "_last_attempt", return_value=False), pytest.raises(RuntimeError, match="host down"):
        _run(action, rid)
    assert _state(factory, rid)[0] == in_progress
    with patch.object(tasks, "_last_attempt", return_value=True), pytest.raises(RuntimeError):
        _run(action, rid)
    state, error = _state(factory, rid)
    assert state == "failed" and "host down" in error


@pytest.mark.parametrize("action", ["stop", "start"])
def test_a_failure_does_not_overwrite_a_range_that_moved_on(factory, backend, action):
    _, in_progress, _ = TASKS[action]
    rid = _range(factory, in_progress)

    async def moved_on(*_):
        with factory() as s:  # an operator abandoned it and destroyed the range meanwhile
            s.get(m.Range, uuid.UUID(rid)).state = m.RangeState.destroying
            s.commit()
        raise RuntimeError("vCenter went away")

    getattr(backend, action).side_effect = moved_on
    with patch.object(tasks, "_last_attempt", return_value=True), pytest.raises(RuntimeError):
        _run(action, rid)
    assert _state(factory, rid)[0] == "destroying"
