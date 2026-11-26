# Author: Marc-Andre Blanke
# Created: 2026-11-26T04:00:00Z
"""TrueNorth Range - where a range build spends its time.

A range build recorded one number, its total, and did not keep it. Nothing said whether the
minutes went to cloning, to Sysprep, to waiting for guest addresses or to the role installs,
so no change to the build could be judged. This records wall-clock seconds per phase, for the
range and for each VM, and the build stores them with the range's provisioner output
(``timings``: provisioners/results.py, worker/range_alloc.stored_output).

Phases are wall time and may overlap: the VMs of a range are built side by side, and the
role installs run beside the Security Onion setup. So a range's phases do not sum to its
total, and a VM's phases sum to that VM's own elapsed time, not to the range's.

No SDK and no I/O here: a clock and a dict.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

# Range phases, in build order (documentation and a stable display order; a phase that did
# not run is absent from the record).
RANGE_PHASES = (
    "prepare",  # folder, port groups, placement
    "vms",  # every VM: clone, customize, power on, guest network, Sysprep (side by side)
    "mirrors",  # port-mirroring sessions
    "roles",  # Windows roles, the forest-root domain controller
    "software_and_products",  # depot installs, Exchange / SharePoint / MECM setup starts
    "securityonion",  # so-setup start (it then runs on its own in the guest)
    "verify_accounts",
    "template_login_check",
)
# Per-VM phases, in order.
VM_PHASES = (
    "queued",  # waiting for a build slot (VSPHERE_CONCURRENCY)
    "create",  # the clone, or the OVF deploy and reconfigure
    "customize",  # Sysprep spec / cloud-init guestinfo / appliance config written
    "power_on",
    "guest_network",  # VMware Tools up and every planned address reported
    "sysprep",  # Windows: waiting for the customization result
)


class _Phases:
    """Seconds per named phase. A phase entered twice adds up."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._seconds: dict[str, float] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time the block, also when it raises: a failed phase still took that long."""
        began = self._clock()
        try:
            yield
        finally:
            self._seconds[name] = self._seconds.get(name, 0.0) + (self._clock() - began)

    def seconds(self) -> dict[str, float]:
        return {name: round(value, 1) for name, value in self._seconds.items()}


class BuildTimings(_Phases):
    """One range build: its own phases, and one set of phases per VM."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        clock = clock or time.monotonic
        super().__init__(clock)
        self._began = clock()
        self._vms: dict[str, _Phases] = {}

    def vm(self, name: str) -> _Phases:
        """The phases of one VM (made on first use)."""
        if name not in self._vms:
            self._vms[name] = _Phases(self._clock)
        return self._vms[name]

    def record(self) -> dict:
        """What is stored: ``{"total_seconds", "phases": {...}, "vms": {name: {...}}}``."""
        return {
            "total_seconds": round(self._clock() - self._began, 1),
            "phases": self.seconds(),
            "vms": {name: phases.seconds() for name, phases in self._vms.items() if phases.seconds()},
        }


def slowest(record: dict, count: int = 3) -> list[str]:
    """The ``count`` longest range phases as ``"name 123s"``, longest first (for one log line)."""
    phases = sorted((record.get("phases") or {}).items(), key=lambda item: item[1], reverse=True)
    return [f"{name} {seconds:.0f}s" for name, seconds in phases[:count]]
