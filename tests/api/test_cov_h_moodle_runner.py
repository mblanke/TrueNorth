"""The Moodle results pull runner (app/moodle_results/runner.py).

The runner is the API-side loop that claims each active Moodle's cursor once per interval
and pulls its results in its own thread. ``pull_all`` takes its session from
``app.db.SessionLocal``, which the tests substitute for a fake session returning the
platform list — the seam the module itself uses — while ``service.pull`` and the loop's
sleep are replaced so the isolation behaviour (one Moodle's busy lease or crash must not
stop the others) can be observed per round. The interval parsing and the disabled loop
run entirely for real.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from app.moodle_results import runner


# ── the interval ───────────────────────────────────────────────────────
def test_the_interval_defaults_to_ten_minutes(monkeypatch):
    monkeypatch.delenv("MOODLE_RESULTS_PULL_SECONDS", raising=False)
    assert runner.interval() == 600.0


@pytest.mark.parametrize("value", ["30", "1.5"])
def test_the_interval_is_read_from_the_environment(monkeypatch, value):
    monkeypatch.setenv("MOODLE_RESULTS_PULL_SECONDS", value)
    assert runner.interval() == float(value)


def test_a_broken_interval_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("MOODLE_RESULTS_PULL_SECONDS", "soon")
    assert runner.interval() == 600.0


# ── one round of pull_all ──────────────────────────────────────────────
class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *criteria):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, platforms):
        self.platforms = platforms
        self.rollbacks = 0
        self.closed = False

    def query(self, model):
        return _FakeQuery(self.platforms)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _run_pull_all(monkeypatch, platforms, pull_behaviour):
    """Patch SessionLocal and service.pull, then run one real pull_all round."""
    db = _FakeDb(platforms)
    monkeypatch.setattr("app.db.SessionLocal", lambda: db)

    pulls = []

    def fake_pull(session, platform):
        pulls.append(platform)
        pull_behaviour(platform)

    monkeypatch.setattr("app.moodle_results.service.pull", fake_pull)
    pulled = runner.pull_all()
    return pulled, pulls, db


def _platform(n):
    return SimpleNamespace(id=f"platform-{n}")


def test_an_empty_platform_list_pulls_nothing(monkeypatch):
    pulled, pulls, db = _run_pull_all(monkeypatch, [], lambda p: None)
    assert pulled == 0 and pulls == []
    assert db.closed is True  # the session is always closed


def test_every_healthy_platform_is_pulled_once(monkeypatch):
    platforms = [_platform(1), _platform(2)]
    pulled, pulls, _ = _run_pull_all(monkeypatch, platforms, lambda p: None)
    assert pulled == 2 and pulls == platforms


def test_a_busy_or_leased_platform_is_skipped_without_a_rollback(monkeypatch):
    from app.moodle_results import service

    platforms = [_platform(1), _platform(2), _platform(3)]

    def behaviour(platform):
        if platform.id == "platform-2":
            raise service.PullBusyError("cursor claimed elsewhere")

    pulled, pulls, db = _run_pull_all(monkeypatch, platforms, behaviour)
    assert pulled == 2 and len(pulls) == 3  # every platform was attempted
    assert db.rollbacks == 0


def test_a_crashing_platform_rolls_back_and_the_rest_still_pull(monkeypatch):
    platforms = [_platform(1), _platform(2), _platform(3)]

    def behaviour(platform):
        if platform.id == "platform-2":
            raise RuntimeError("moodle exploded")

    pulled, pulls, db = _run_pull_all(monkeypatch, platforms, behaviour)
    assert pulled == 2 and len(pulls) == 3
    assert db.rollbacks == 1
    assert db.closed is True


# ── the loop ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_interval_of_zero_disables_the_loop(monkeypatch):
    monkeypatch.setenv("MOODLE_RESULTS_PULL_SECONDS", "0")
    await runner.loop()  # returns without ever sleeping or pulling


@pytest.mark.asyncio
async def test_the_loop_runs_rounds_and_outlives_a_failed_round(monkeypatch):
    monkeypatch.setattr(runner, "interval", lambda: 300.0)

    sleeps = []
    rounds = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:  # two rounds, then stop the loop
            raise asyncio.CancelledError

    async def fake_to_thread(fn):
        rounds.append("round")
        if len(rounds) == 1:
            raise RuntimeError("first round exploded")
        return 0

    monkeypatch.setattr(runner, "asyncio", SimpleNamespace(sleep=fake_sleep, to_thread=fake_to_thread))

    with pytest.raises(asyncio.CancelledError):
        await runner.loop()
    assert sleeps == [300.0, 300.0, 300.0]
    assert len(rounds) == 2  # the crashed first round did not end the loop
