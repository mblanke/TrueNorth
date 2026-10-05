"""The range state machine: which state a range may move to from which.

``stopping``/``starting`` (S5a) are the in-progress states of a power operation, as
``provisioning``/``destroying`` are for a build or teardown: the API moves the range into
one when it accepts the operation (app/range_ops.py), and only the worker, having powered
the VMs, writes ``stopped``/``running`` (or ``failed``) from it (worker/fencing.py). The
API never claims a power state itself. A failed range may be started or stopped again,
so a power failure (a host down, vCenter unreachable) does not force a rebuild.
"""

from __future__ import annotations

RANGE_TRANSITIONS: dict[str, list[str]] = {
    "created": ["provisioning", "destroyed"],
    "provisioning": ["ready", "failed"],
    # A provisioned range's VMs are already powered on, so it can be stopped straight from ready.
    "ready": ["running", "stopping", "destroying"],
    "running": ["stopping", "destroying"],
    "stopping": ["stopped", "failed"],
    "stopped": ["starting", "destroying"],
    "starting": ["running", "failed"],
    "destroying": ["destroyed", "failed"],
    "failed": ["provisioning", "starting", "stopping", "destroying", "destroyed"],
}
