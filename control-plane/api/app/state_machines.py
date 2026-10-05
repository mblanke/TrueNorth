"""Allowed state transitions for ranges and exercises (``RangeState.can_transition_to``).

Kept out of models.py, whose line count is capped (ADR 0003).

Range power (codereview1 S0 defect): ``stopping``/``starting`` are held while a stop or
start operation is with the worker (app/range_ops.py). The worker leaves them for where
the VMs are: ``stopped``/``ready`` on success; ``ready``/``stopped`` (the state before)
on failure, with the error; ``failed`` only by an operator abandoning the operation.
``running`` is legacy: nothing sets it, and it behaves like ``ready``.
"""

_RANGE_TRANSITIONS: dict[str, list[str]] = {
    "created": ["provisioning", "destroyed"],
    "provisioning": ["ready", "failed"],
    "ready": ["running", "stopping", "destroying"],
    "running": ["stopping", "destroying"],
    "stopping": ["stopped", "ready", "failed"],
    "stopped": ["starting", "destroying"],
    "starting": ["ready", "stopped", "failed"],
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
