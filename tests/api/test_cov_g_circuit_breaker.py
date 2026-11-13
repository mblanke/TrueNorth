"""Round-3 coverage for app/circuit_breaker.py (whole-slice rank: 64.0% before this file).

The breaker around every external call (Proxmox, Keycloak, OpenSearch) was only reached
incidentaly by the slice's tests. These tests drive the real state machine end to end:
a closed circuit passes calls through and forgets a non-consecutive failure streak,
the threshold trip to OPEN with a rejected call carrying a retry hint, the timed probe
to HALF_OPEN, a failed probe re-opening, and recovery back to CLOSED only after the
success threshold. Timing is real, with a sub-100ms recovery timeout.
"""

from __future__ import annotations

import asyncio

import pytest
from app.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState

pytestmark = pytest.mark.asyncio


async def failing(exc: Exception):
    raise exc


# -- closed --------------------------------------------------------------------------------------


async def test_a_closed_circuit_passes_calls_through_with_their_arguments():
    seen = {}
    breaker = CircuitBreaker("proxmox")

    async def work(*args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return "payload"

    assert await breaker.call(work, 7, node="p1") == "payload"
    assert seen == {"args": (7,), "kwargs": {"node": "p1"}}
    assert breaker.state is CircuitState.closed


async def test_a_success_resets_the_failure_streak():
    breaker = CircuitBreaker("keycloak", failure_threshold=2)

    with pytest.raises(RuntimeError):
        await breaker.call(failing, RuntimeError("flaky"))
    await breaker.call(asyncio.sleep, 0)  # a success between the failures
    with pytest.raises(RuntimeError):
        await breaker.call(failing, RuntimeError("flaky again"))

    assert breaker.state is CircuitState.closed  # never two consecutive failures


# -- the trip to OPEN ----------------------------------------------------------------------------


async def test_threshold_failures_trip_the_circuit_open():
    breaker = CircuitBreaker("opensearch", failure_threshold=3)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await breaker.call(failing, RuntimeError("cluster down"))

    assert breaker.state is CircuitState.open


async def test_an_open_circuit_rejects_without_calling_through():
    breaker = CircuitBreaker("keycloak", failure_threshold=1)
    with pytest.raises(RuntimeError):
        await breaker.call(failing, RuntimeError("kc down"))

    called = []

    async def work():
        called.append(1)
        return "should not happen"

    with pytest.raises(CircuitOpenError) as raised:
        await breaker.call(work)

    assert called == []  # the call never reached the service
    assert raised.value.name == "keycloak"
    assert raised.value.retry_after > 0
    assert "Circuit 'keycloak' is OPEN" in str(raised.value)


# -- the probe -----------------------------------------------------------------------------------


async def test_after_the_recovery_timeout_a_probe_is_allowed():
    breaker = CircuitBreaker("proxmox", failure_threshold=1, recovery_timeout=0.05)
    with pytest.raises(RuntimeError):
        await breaker.call(failing, RuntimeError("pve down"))
    await asyncio.sleep(0.06)

    assert await breaker.call(asyncio.sleep, 0) is None
    assert breaker.state is CircuitState.half_open


async def test_a_failed_probe_re_opens_the_circuit():
    breaker = CircuitBreaker("proxmox", failure_threshold=1, recovery_timeout=0.05)
    with pytest.raises(RuntimeError):
        await breaker.call(failing, RuntimeError("pve down"))
    await asyncio.sleep(0.06)

    with pytest.raises(RuntimeError):
        await breaker.call(failing, RuntimeError("probe failed too"))

    assert breaker.state is CircuitState.open


async def test_recovery_needs_consecutive_probe_successes():
    breaker = CircuitBreaker("keycloak", failure_threshold=1, recovery_timeout=0.05, success_threshold=2)
    with pytest.raises(RuntimeError):
        await breaker.call(failing, RuntimeError("kc down"))
    await asyncio.sleep(0.06)

    await breaker.call(asyncio.sleep, 0)
    assert breaker.state is CircuitState.half_open  # one success is not enough

    await breaker.call(asyncio.sleep, 0)
    assert breaker.state is CircuitState.closed


# -- the manual reset ----------------------------------------------------------------------------


async def test_reset_forces_the_breaker_closed():
    breaker = CircuitBreaker("opensearch", failure_threshold=1)
    with pytest.raises(RuntimeError):
        await breaker.call(failing, RuntimeError("down"))

    assert breaker.state is CircuitState.open
    breaker.reset()

    assert breaker.state is CircuitState.closed
