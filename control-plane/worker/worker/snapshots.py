"""Snapshot naming and the states the snapshot tasks (tasks.py) write from.

Kept out of tasks.py, whose line count is capped (ADR 0003).
"""

from __future__ import annotations

# States routers/ranges.py:restore_snapshot accepts. A restore only ever writes to a
# range still in one of them; if the range moved on (say, it was destroyed while the
# task queued or retried), the range is left alone.
_RESTORABLE_STATES = ("ready", "stopped", "failed")
# Snapshot states the snapshot task may still write over: its first attempt, or a retry.
_SNAPSHOT_PENDING = ("creating", "failed")


def _backend_snapshot_name(snapshot_id: str) -> str:
    """The name the hypervisor stores the snapshot under.

    Proxmox requires a snapname that starts with a letter, uses only letters, digits,
    ``-`` and ``_``, and is at most 40 characters. The bare UUID used before starts
    with a digit ten times in sixteen, and Proxmox rejected those.
    """
    return "tn" + "".join(ch for ch in snapshot_id if ch.isalnum())[:38]
