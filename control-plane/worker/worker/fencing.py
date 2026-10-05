"""Range tasks act only for the operation the API recorded.

The API records a provision or destroy by moving the range into ``provisioning`` or
``destroying`` in the same transaction as the operation row (control-plane/api/app/
range_ops.py). A task may be delivered twice (worker loss with late acks, or the API's
outbox re-sending) or late, after the range has moved on. So a task claims the range
with a conditional update from that in-progress state and does nothing if the claim
matches no row; its outcome is written only from that state too. A retry of a failed
attempt still finds the range in progress, because only the last attempt records
``failed`` (tasks._last_attempt).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("worker.fencing")


class PermanentError(RuntimeError):
    """A failure no retry can fix (a full pool, a range with nothing to power): the task
    records ``failed`` on the first attempt instead of retrying (tasks.ReliableTask)."""


def skipped(action: str, range_id: str, expected: str) -> dict:
    """The result of a duplicate or stale delivery: logged, and nothing else."""
    logger.warning("[%s] range %s is no longer %s: duplicate or stale delivery, skipped", action, range_id, expected)
    return {"status": "skipped", "range_id": range_id, "reason": f"range is no longer {expected}"}
