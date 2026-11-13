"""The nightly AD status sync clock (app/ad_status_sync/clock.py).

An asyncio task started from the API lifespan: sleep a tick, skip while Keycloak's Admin
API is unconfigured, otherwise run the night's work in a thread with its own session and
relay the outcome's mails and invites through the classes and scheduler senders. The
clock's collaborators — the sleep/to_thread pair it takes from asyncio, the directory
gate, the nightly service and the two senders — are patched per test so the loop's
decisions are observed over several ticks; the tick work itself and the loop's resilience
to a crashed tick are what is under test.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from app.ad_status_sync import clock


def _stub_loop(monkeypatch, *, ticks: int, configured: bool, tick_result):
    """Patch the clock's asyncio, the directory gate and the senders; run `ticks` rounds."""
    sleeps = []
    tick_calls = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= ticks:
            raise asyncio.CancelledError  # how the test stops the otherwise endless loop

    async def fake_to_thread(fn):
        tick_calls.append(fn)
        if isinstance(tick_result, Exception):
            raise tick_result
        return tick_result

    sent = []

    async def fake_send(mails):
        sent.append(("mails", mails))

    async def fake_send_invites(invites):
        sent.append(("invites", invites))

    monkeypatch.setattr(
        clock, "asyncio",
        SimpleNamespace(sleep=fake_sleep, to_thread=fake_to_thread, CancelledError=asyncio.CancelledError),
    )
    monkeypatch.setattr(clock, "TICK_SECONDS", 0.0)
    monkeypatch.setattr("app.ad_status_sync.directory.configured", lambda: configured)
    monkeypatch.setattr("app.classes.service.send", fake_send)
    monkeypatch.setattr("app.scheduler.service.send_invites", fake_send_invites)
    return sleeps, tick_calls, sent


@pytest.mark.asyncio
async def test_while_the_directory_is_not_configured_the_clock_only_sleeps(monkeypatch):
    sleeps, tick_calls, sent = _stub_loop(monkeypatch, ticks=3, configured=False, tick_result=None)
    with pytest.raises(asyncio.CancelledError):
        await clock.run_forever()
    assert sleeps == [0.0, 0.0, 0.0]
    assert tick_calls == [] and sent == []


@pytest.mark.asyncio
async def test_a_tick_with_no_outcome_sends_nothing(monkeypatch):
    sleeps, tick_calls, sent = _stub_loop(monkeypatch, ticks=2, configured=True, tick_result=None)
    with pytest.raises(asyncio.CancelledError):
        await clock.run_forever()
    assert len(tick_calls) == 1 and sent == []


@pytest.mark.asyncio
async def test_a_night_that_ran_relays_the_mails_and_the_invites(monkeypatch):
    outcome = SimpleNamespace(mails=["mail-1"], invites=["invite-1"])
    sleeps, tick_calls, sent = _stub_loop(monkeypatch, ticks=2, configured=True, tick_result=outcome)
    with pytest.raises(asyncio.CancelledError):
        await clock.run_forever()
    assert sent == [("mails", ["mail-1"]), ("invites", ["invite-1"])]


@pytest.mark.asyncio
async def test_a_crashed_tick_is_logged_and_the_next_tick_still_runs(monkeypatch, caplog):
    sleeps, tick_calls, sent = _stub_loop(
        monkeypatch, ticks=3, configured=True, tick_result=RuntimeError("database gone")
    )
    with caplog.at_level(logging.ERROR, logger="truenorth.api.ad_status_sync.clock"), pytest.raises(asyncio.CancelledError):
        await clock.run_forever()
    assert len(tick_calls) == 2  # tick 1 crashed, tick 2 ran anyway, tick 3 never slept
    assert "AD status sync tick failed" in caplog.text
    assert sent == []


def test_the_tick_runs_the_nightly_service_in_its_own_session(monkeypatch):
    seen = {}

    class FakeDb:
        def close(self):
            seen["closed"] = True

    monkeypatch.setattr("app.db.SessionLocal", lambda: FakeDb())

    def fake_run_nightly(db, reader):
        seen["db"] = db
        seen["reader"] = type(reader).__name__
        return "outcome"

    monkeypatch.setattr("app.ad_status_sync.service.run_nightly", fake_run_nightly)

    assert clock._tick_in_own_session() == "outcome"
    assert seen["db"].__class__ is FakeDb and seen["closed"] is True
    assert seen["reader"] == "KeycloakDirectoryReader"
