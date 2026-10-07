"""The tasks.py split (ADR 0003) changed no Celery name, and the run loop calls the inject seam.

run_scenario / run_scenario_v2 moved to worker/exercise_run.py, generate_aar to
worker/aar_tasks.py and ingest_telemetry_batch to worker/telemetry_tasks.py. Their registered names are a contract with the API (contracts.py)
and with messages already queued, so they must stay ``worker.tasks.<name>``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("celery")

from worker import aar_tasks, db_ops, exercise_run, inject_dispatch, tasks, telemetry_tasks  # noqa: E402
from worker.celery_app import app  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "control-plane" / "worker"

# Every task the worker registered before the split. A rename here breaks the API's
# dispatch and strands messages already on the broker.
REGISTERED_BEFORE_SPLIT = {
    "worker.tasks.auto_assess_competency",
    "worker.tasks.batch_provision",
    "worker.tasks.collect_range_metrics",
    "worker.tasks.delete_snapshot",
    "worker.tasks.destroy_range",
    "worker.tasks.forge_exercise",
    "worker.tasks.generate_aar",
    "worker.tasks.generate_learning_recommendation",
    "worker.tasks.health_check_ranges",
    "worker.tasks.ingest_telemetry_batch",
    "worker.tasks.provision_range",
    "worker.tasks.reconcile_lab_vms",
    "worker.tasks.restore_snapshot",
    "worker.tasks.run_scenario",
    "worker.tasks.run_scenario_v2",
    "worker.tasks.snapshot_range",
    "worker.tasks.start_range",
    "worker.tasks.stop_range",
}


def _registered() -> set[str]:
    app.finalize()
    return {n for n in app.tasks if n.startswith("worker.")}


def test_registered_task_names_are_unchanged():
    assert _registered() == REGISTERED_BEFORE_SPLIT


@pytest.mark.parametrize(
    ("module", "name", "reliable"),
    [
        (exercise_run, "run_scenario", False),
        (exercise_run, "run_scenario_v2", True),
        (aar_tasks, "generate_aar", True),
        (telemetry_tasks, "ingest_telemetry_batch", False),
    ],
)
def test_moved_tasks_keep_name_base_and_old_import_path(module, name, reliable):
    from worker.base_tasks import ReliableTask

    task = getattr(module, name)
    registered = app.tasks[f"worker.tasks.{name}"]
    assert task.name == registered.name == f"worker.tasks.{name}"
    assert registered.run.__module__ == module.__name__  # the moved function is what runs
    assert isinstance(registered, ReliableTask) is reliable  # retry policy unchanged
    assert getattr(tasks, name) is task  # `from worker.tasks import ...` still works


def test_unknown_attribute_on_tasks_still_raises():
    with pytest.raises(AttributeError):
        tasks.no_such_task  # noqa: B018


@pytest.mark.parametrize(
    "module",
    [
        "worker.exercise_run",
        "worker.aar_tasks",
        "worker.task_plumbing",
        "worker.inject_dispatch",
        "worker.tasks",
        "worker.telemetry_tasks",
    ],
)
def test_module_imports_first_in_a_fresh_interpreter(module):
    """celery_app imports every task module at its end; a moved module imported first
    must not find tasks.py (or itself) half-initialised. In-process imports cannot
    catch this, hence the subprocess."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(WORKER), os.environ.get("PYTHONPATH", "")])}
    code = f"import {module}; from worker.tasks import run_scenario_v2, generate_aar; print(run_scenario_v2.name)"
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "worker.tasks.run_scenario_v2"


def test_seam_is_a_no_op_until_wired():
    assert inject_dispatch.dispatch_inject("ex-1", "deploy_malware", {"target": "ws-001"}) == {
        "dispatched": False,
        "reason": "not wired",
    }


def _session():
    s = MagicMock()
    s.__enter__ = MagicMock(return_value=s)
    s.__exit__ = MagicMock(return_value=False)
    return s


TIMELINE = [
    {"t": "0:00", "action": "deploy_malware", "params": {"target": "ws-001"}},
    {"t": "0:00", "action": "exfil_data", "params": {"target": "dc-01"}},
    {"t": "0:00", "action": "cleanup"},
]


@pytest.fixture
def real_backend(monkeypatch):
    """Off the mock backend (where the stub comment was), with detection scoring off."""
    monkeypatch.setenv("PROVISIONER_BACKEND", "vsphere_api")
    monkeypatch.delenv("DETECTION_SCORING", raising=False)


def test_run_scenario_v2_calls_the_seam_once_per_inject(real_backend):
    """Each timeline event goes through dispatch_inject exactly once, in order, with the exercise id."""
    definition = {"timeline": TIMELINE, "objectives": [], "inject_packs": []}
    with (
        patch("worker.tasks._db_session", return_value=_session()),
        patch("worker.tasks._notify_api"),
        patch("worker.inject_dispatch.dispatch_inject", return_value={"dispatched": False}) as seam,
    ):
        result = exercise_run.run_scenario_v2(exercise_id="ex-seam", scenario_definition=definition)
    assert result["events_executed"] == 3
    assert [c.args for c in seam.call_args_list] == [
        ("ex-seam", "deploy_malware", {"target": "ws-001"}),
        ("ex-seam", "exfil_data", {"target": "dc-01"}),
        ("ex-seam", "cleanup", {}),
    ]


def test_run_scenario_v2_on_the_mock_backend_does_not_call_the_seam(monkeypatch):
    """Behaviour-preserving: the stub sat on the non-mock branch only."""
    monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
    definition = {"timeline": TIMELINE, "objectives": [], "inject_packs": []}
    with (
        patch("worker.tasks._db_session", return_value=_session()),
        patch("worker.tasks._notify_api"),
        patch("worker.inject_dispatch.dispatch_inject") as seam,
    ):
        result = exercise_run.run_scenario_v2(exercise_id="ex-mock", scenario_definition=definition)
    assert result["events_executed"] == 3
    seam.assert_not_called()


def test_run_scenario_v2_result_ignores_the_seams_answer(real_backend):
    """The not-wired answer neither fails nor skips an event."""
    definition = {"timeline": TIMELINE, "objectives": [], "inject_packs": []}
    with patch("worker.tasks._db_session", return_value=_session()), patch("worker.tasks._notify_api") as notify:
        result = exercise_run.run_scenario_v2(exercise_id="ex-nw", scenario_definition=definition)
    assert result == {"status": "completed", "exercise_id": "ex-nw", "events_executed": 3, "objectives_completed": 0}
    assert notify.call_args_list[-1].args[1]["state"] == "completed"


def test_run_scenario_v2_seam_failure_cancels_the_exercise(real_backend):
    """A seam that raises takes the existing failure path: exercise cancelled, failed
    notification, exception re-raised for the retry policy; never marked complete."""
    session = _session()
    definition = {"timeline": TIMELINE, "objectives": [], "inject_packs": []}
    with (
        patch("worker.tasks._db_session", return_value=session),
        patch("worker.tasks._notify_api") as notify,
        patch("worker.inject_dispatch.dispatch_inject", side_effect=RuntimeError("injector down")),
        patch.object(db_ops, "cancel_exercise") as cancel,
        patch.object(db_ops, "complete_exercise") as complete,
        pytest.raises(RuntimeError, match="injector down"),
    ):
        exercise_run.run_scenario_v2(exercise_id="ex-fail", scenario_definition=definition)
    cancel.assert_called_once_with(session, "ex-fail")
    complete.assert_not_called()
    assert notify.call_args_list[-1].args[1] == {"id": "ex-fail", "state": "failed", "error": "injector down"}


def test_run_scenario_calls_the_seam_once_per_inject():
    yaml_doc = "timeline:\n" + "".join(f"  - {{t: '0:00', action: {e['action']}}}\n" for e in TIMELINE)
    with (
        patch("worker.tasks._db_session", return_value=_session()),
        patch.object(db_ops, "exercise_scenario_yaml", return_value=("ex-v1", yaml_doc)),
        patch("worker.inject_dispatch.dispatch_inject") as seam,
    ):
        result = exercise_run.run_scenario(exercise_id="ex-v1")
    assert result == {"status": "completed", "exercise_id": "ex-v1", "events_executed": 3}
    assert [c.args[:2] for c in seam.call_args_list] == [
        ("ex-v1", "deploy_malware"),
        ("ex-v1", "exfil_data"),
        ("ex-v1", "cleanup"),
    ]


def test_moved_tasks_still_use_the_patched_plumbing_in_tasks():
    """task_plumbing resolves tasks._db_session / _notify_api at call time, so the
    existing patch("worker.tasks._db_session") tests still cover the moved code."""
    with (
        patch("worker.tasks._db_session", return_value=_session()) as db,
        patch("worker.tasks._notify_api") as notify,
        patch.object(db_ops, "exercise_for_aar", return_value=None),
        pytest.raises(ValueError, match="not found"),
    ):
        aar_tasks.generate_aar(exercise_id="ex-missing")
    db.assert_called_once()
    assert notify.call_args_list[-1].args[1]["event"] == "aar_failed"
