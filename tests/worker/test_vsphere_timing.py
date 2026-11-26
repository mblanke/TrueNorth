# Author: Marc-Andre Blanke
# Created: 2026-11-26T04:00:00Z
"""Where a range build spends its time (provisioners/vsphere_timing.py), and that a vSphere
build records it and the worker stores it with the range."""

from __future__ import annotations

import pytest
from test_vsphere_provision import (  # noqa: F401  (vc, worker_reserves: fixtures; the second is autouse)
    ONE_LINUX,
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


def _slow(prov, clock, name, seconds, *, is_async=False):
    """Make ``prov.<name>`` take ``seconds`` on ``clock`` (and still do its work)."""
    real = getattr(prov, name)
    if is_async:

        async def slow(*args, **kwargs):
            clock.now += seconds
            return await real(*args, **kwargs)
    else:

        def slow(*args, **kwargs):
            clock.now += seconds
            return real(*args, **kwargs)

    setattr(prov, name, slow)


def test_each_timer_is_on_the_step_it_names(vc):  # noqa: F811
    """Every timed step is made to take its own number of seconds on a clock the test
    advances. A timer around the wrong block, or two swapped, shows as the wrong number."""

    clock = _Clock()
    prov = _prov(vc)
    prov._timing_clock = clock
    _slow(prov, clock, "_prepare_sync", 5)
    _slow(prov, clock, "_clone_sync", 60)
    _slow(prov, clock, "_customize_sync", 7)
    _slow(prov, clock, "_power_action", 3, is_async=True)
    _slow(prov, clock, "_await_guest_network", 40, is_async=True)
    _slow(prov, clock, "_install_roles", 300, is_async=True)
    _slow(prov, clock, "_install_software", 90, is_async=True)
    _slow(prov, clock, "_setup_securityonion", 11, is_async=True)
    _slow(prov, clock, "_verify_accounts", 13, is_async=True)
    _slow(prov, clock, "_check_template_logins", 17, is_async=True)

    # One VM: with several, one VM's wait for its guest network runs beside the next VM's
    # clone on the same clock, and each would count the other's seconds (phases overlap).
    result = _run(prov.provision(RANGE_ID, _rendered(ONE_LINUX), {}))
    assert result.status == "ok", result.errors
    phases, vms = result.timings["phases"], result.timings["vms"]
    for name, seconds in (("prepare", 5), ("securityonion", 11), ("verify_accounts", 13), ("template_login_check", 17)):
        assert phases[name] == seconds, (name, phases)
    # The Security Onion setup runs beside the roles and the software, on the same clock, so
    # its 11 s land inside whichever of the two is open when it runs.
    assert phases["roles"] in (300, 311) and phases["software_and_products"] in (90, 101), phases
    assert phases["roles"] + phases["software_and_products"] == 300 + 90 + 11
    (phases_of_vm,) = vms.values()
    assert phases_of_vm == {"queued": 0.0, "create": 60.0, "customize": 7.0, "power_on": 3.0, "guest_network": 40.0}, (
        phases_of_vm
    )
    assert phases["vms"] == 60 + 7 + 3 + 40
    assert result.timings["total_seconds"] == 5 + 110 + 300 + 90 + 11 + 13 + 17


def test_the_build_slot_still_bounds_how_many_vms_are_created_at_once(vc):  # noqa: F811
    """Timing the wait for a build slot must not have removed the slot (VSPHERE_CONCURRENCY):
    with one slot, no two VMs are ever between "create" and "power on" at the same time."""
    import asyncio

    prov = _prov(vc)
    prov._semaphore = asyncio.Semaphore(1)
    inside, most = 0, 0
    real_clone, real_power = prov._clone_sync, prov._power_action

    def clone(*args, **kwargs):
        nonlocal inside, most
        inside += 1
        most = max(most, inside)
        return real_clone(*args, **kwargs)

    async def power(client, vm_id, action):
        nonlocal inside
        await asyncio.sleep(0)  # let another VM run here if nothing holds it back
        await asyncio.sleep(0)
        result = await real_power(client, vm_id, action)
        inside -= 1
        return result

    prov._clone_sync, prov._power_action = clone, power
    result = _run(prov.provision(RANGE_ID, _rendered(STARTER), {}))
    assert result.status == "ok", result.errors
    assert len(result.vms) == 3 and most == 1 and inside == 0
