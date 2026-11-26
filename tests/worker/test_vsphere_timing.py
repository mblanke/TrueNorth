# Author: Marc-Andre Blanke
# Created: 2026-11-26T04:00:00Z
"""Where a range build spends its time (provisioners/vsphere_timing.py), and that a vSphere
build records it and the worker stores it with the range."""

from __future__ import annotations

import pytest
from test_vsphere_provision import (  # noqa: F401  (vc, worker_reserves: fixtures; the second is autouse)
    R8,
    RANGE_ID,
    STARTER,
    _prov,
    _rendered,
    _run,
    vc,
    worker_reserves,
)
from worker import range_alloc
from worker.provisioners import vsphere_timing as timing


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_phases_are_wall_seconds_and_a_repeated_phase_adds_up():
    clock = _Clock()
    timed = timing.BuildTimings(clock)
    with timed.phase("prepare"):
        clock.now += 2.04
    with timed.phase("roles"):
        clock.now += 30
    with timed.phase("roles"):
        clock.now += 12.5
    vm = timed.vm("dc01")
    with vm.phase("create"):
        clock.now += 61.26
    assert timed.vm("dc01") is vm
    timed.vm("never-timed")  # asked for, nothing timed: left out of the record
    assert timed.record() == {
        "total_seconds": 105.8,
        "phases": {"prepare": 2.0, "roles": 42.5},
        "vms": {"dc01": {"create": 61.3}},
    }


def test_a_phase_that_raises_is_still_timed():
    clock = _Clock()
    timed = timing.BuildTimings(clock)
    with pytest.raises(RuntimeError), timed.phase("vms"):
        clock.now += 7
        raise RuntimeError("clone failed")
    assert timed.record()["phases"] == {"vms": 7.0}


def test_slowest_names_the_longest_phases_first():
    record = {"phases": {"prepare": 3.0, "vms": 410.2, "roles": 1290.7, "verify_accounts": 9.9}}
    assert timing.slowest(record) == ["roles 1291s", "vms 410s", "verify_accounts 10s"]
    assert timing.slowest({}) == []


def test_the_documented_phase_names_are_the_ones_a_build_records(vc):  # noqa: F811
    """A build of the starter range: its range phases and each VM's phases are recorded under
    the documented names, and the worker stores them with the range."""
    result = _run(_prov(vc).provision(RANGE_ID, _rendered(STARTER), {}))
    assert result.status == "ok", result.errors
    timings = result.timings
    assert timings["total_seconds"] >= 0
    assert {"prepare", "vms", "verify_accounts", "template_login_check"} <= set(timings["phases"])
    assert set(timings["phases"]) <= set(timing.RANGE_PHASES)
    assert set(timings["vms"]) == {v["name"] for v in result.vms} and f"{R8}-ubu02" in timings["vms"]
    for phases in timings["vms"].values():
        assert {"queued", "create", "customize", "power_on", "guest_network"} <= set(phases)
        assert set(phases) <= set(timing.VM_PHASES)
        assert all(isinstance(seconds, float) and seconds >= 0 for seconds in phases.values())
    assert range_alloc.stored_output("vsphere_api", RANGE_ID, result)["timings"] == timings
