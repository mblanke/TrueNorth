"""Allowed state transitions for ranges and exercises (kept out of models.py, ADR 0003).

A range's in-progress states (provisioning, destroying, stopping, starting) are set by
the API when it records a request, and left only by the worker, which reports the
outcome (worker/fencing.py): the API never claims a power state the VMs are not in.
"""

from __future__ import annotations

_RANGE_TRANSITIONS: dict[str, list[str]] = {
    "created": ["provisioning", "destroyed"],
    "provisioning": ["ready", "failed"],
    "ready": ["running", "stopping", "destroying"],
    "running": ["stopping", "destroying"],
    "stopping": ["stopped", "failed"],
    "stopped": ["starting", "destroying"],
    "starting": ["running", "failed"],
    "destroying": ["destroyed", "failed"],
    "failed": ["provisioning", "destroying", "destroyed"],
}

_EXERCISE_TRANSITIONS: dict[str, list[str]] = {
    "pending": ["running", "cancelled"],
    "running": ["paused", "completed", "cancelled"],
    "paused": ["running", "cancelled"],
    "completed": [],
    "cancelled": [],
}
