"""Round-3 coverage for worker/chaos.py (whole-slice rank: 50% before this file).

The Celery chaos hooks are registered at import but never driven by the slice's tests.
These tests call the real signal-receiver functions with configuration set through the
module's own knobs: only targeted tasks are affected, latency is drawn and slept, a
losing draw raises ChaosError out of the prerun hook, completions of affected tasks are
logged, and the health-endpoint status mirrors the configuration. The random draws and
the sleep are pinned at the module's own `random`/`time` seams so each branch is shown
deterministically.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from worker import chaos

TASK = SimpleNamespace(name="worker.tasks.provision_range")


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    """Every test starts from chaos off, so each test turns on exactly what it shows."""
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", False)
    monkeypatch.setattr(chaos, "CHAOS_FAILURE_RATE", 0.0)
    monkeypatch.setattr(chaos, "CHAOS_LATENCY_MS", 0)
    monkeypatch.setattr(chaos, "CHAOS_AFFECTED_TASKS", None)


# -- task targeting ----------------------------------------------------------------------


def test_disabled_chaos_applies_to_nothing():
    assert chaos._should_apply("worker.tasks.provision_range") is False
    assert chaos._should_apply("anything") is False


def test_enabled_chaos_without_a_target_list_applies_to_every_task(monkeypatch):
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", True)
    assert chaos._should_apply("worker.tasks.provision_range") is True
    assert chaos._should_apply("worker.tasks.sweep_range") is True


def test_enabled_chaos_with_a_target_list_only_applies_to_members(monkeypatch):
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", True)
    monkeypatch.setattr(chaos, "CHAOS_AFFECTED_TASKS", {"worker.tasks.sweep_range"})
    assert chaos._should_apply("worker.tasks.sweep_range") is True
    assert chaos._should_apply("worker.tasks.provision_range") is False


# -- the prerun hook ---------------------------------------------------------------------


def test_a_hook_without_a_sender_is_a_no_op(monkeypatch):
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", True)
    monkeypatch.setattr(chaos, "CHAOS_FAILURE_RATE", 1.0)  # would fire if the guard were wrong
    chaos.chaos_prerun_hook(sender=None, task_id="t1")


def test_an_unaffected_task_is_left_alone_even_at_full_failure_rate(monkeypatch):
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", True)
    monkeypatch.setattr(chaos, "CHAOS_FAILURE_RATE", 1.0)
    monkeypatch.setattr(chaos, "CHAOS_AFFECTED_TASKS", {"worker.tasks.sweep_range"})

    chaos.chaos_prerun_hook(sender=TASK, task_id="t1")  # no raise


def test_a_zero_latency_draw_sleeps_and_warns_nothing(monkeypatch, caplog):
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", True)
    monkeypatch.setattr(chaos, "CHAOS_LATENCY_MS", 50)
    monkeypatch.setattr(chaos.random, "randint", lambda a, b: 0)

    with caplog.at_level(logging.WARNING, logger="truenorth.chaos"):
        chaos.chaos_prerun_hook(sender=TASK, task_id="t1")

    assert caplog.text == ""


def test_a_latency_draw_is_slept_in_milliseconds_and_warned(monkeypatch, caplog):
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", True)
    monkeypatch.setattr(chaos, "CHAOS_LATENCY_MS", 50)
    monkeypatch.setattr(chaos.random, "randint", lambda a, b: 40)
    slept = []
    monkeypatch.setattr(chaos.time, "sleep", slept.append)

    with caplog.at_level(logging.WARNING, logger="truenorth.chaos"):
        chaos.chaos_prerun_hook(sender=TASK, task_id="t1")

    assert slept == [0.04]
    assert any("Injecting 40ms latency" in r.message and "t1" in r.message for r in caplog.records), caplog.text


def test_a_losing_failure_draw_raises_chaos_error_out_of_the_hook(monkeypatch):
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", True)
    monkeypatch.setattr(chaos, "CHAOS_FAILURE_RATE", 0.5)
    monkeypatch.setattr(chaos.random, "random", lambda: 0.1)

    with pytest.raises(chaos.ChaosError) as raised:
        chaos.chaos_prerun_hook(sender=TASK, task_id="t1")

    assert "worker.tasks.provision_range" in str(raised.value)
    assert "failure_rate=0.5" in str(raised.value)


def test_a_winning_failure_draw_lets_the_task_run(monkeypatch):
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", True)
    monkeypatch.setattr(chaos, "CHAOS_FAILURE_RATE", 0.5)
    monkeypatch.setattr(chaos.random, "random", lambda: 0.9)

    chaos.chaos_prerun_hook(sender=TASK, task_id="t1")  # no raise


# -- the postrun hook --------------------------------------------------------------------


def test_a_completed_affected_task_is_logged_with_its_state(monkeypatch, caplog):
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", True)

    with caplog.at_level(logging.INFO, logger="truenorth.chaos"):
        chaos.chaos_postrun_hook(sender=TASK, task_id="t1", state="SUCCESS")

    assert any(
        "Task worker.tasks.provision_range (t1) completed with state=SUCCESS" in r.message
        for r in caplog.records
    ), caplog.text


def test_postrun_logs_nothing_while_chaos_is_disabled(caplog):
    with caplog.at_level(logging.INFO, logger="truenorth.chaos"):
        chaos.chaos_postrun_hook(sender=TASK, task_id="t1", state="SUCCESS")

    assert caplog.text == ""


# -- the health status -------------------------------------------------------------------


def test_the_status_reports_the_off_configuration():
    assert chaos.get_chaos_status() == {
        "enabled": False,
        "failure_rate": 0.0,
        "latency_ms": 0,
        "affected_tasks": "all",
    }


def test_the_status_reports_targets_as_a_list_and_the_live_numbers(monkeypatch):
    monkeypatch.setattr(chaos, "CHAOS_ENABLED", True)
    monkeypatch.setattr(chaos, "CHAOS_FAILURE_RATE", 0.25)
    monkeypatch.setattr(chaos, "CHAOS_LATENCY_MS", 200)
    monkeypatch.setattr(chaos, "CHAOS_AFFECTED_TASKS", {"worker.tasks.sweep_range", "worker.tasks.provision_range"})

    status = chaos.get_chaos_status()

    assert status["enabled"] is True
    assert status["failure_rate"] == 0.25 and status["latency_ms"] == 200
    assert sorted(status["affected_tasks"]) == [
        "worker.tasks.provision_range",
        "worker.tasks.sweep_range",
    ]
