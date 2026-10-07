"""The worker does not score detections (ADR 0005).

Detection credit comes only from what the Student did: a Student submits a detection to the
API (``app/routers/detections.py``), which checks it against the range's events inside the
exercise window and records the credit (``app/detections/credit.py``). The worker's old
telemetry scorer credited the inject's own events to a Student who did nothing, so it is
gone, and ``DETECTION_SCORING`` no longer turns anything on.

What remains is the seam ``tasks.py`` still imports: ``range_index`` (the range's event
index, owned by ``telemetry.py``) and ``detection_scorer``, which always answers "no scorer".
A run on a real backend therefore achieves no objective by itself.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from .telemetry import range_index

__all__ = ["detection_scorer", "range_index"]

logger = logging.getLogger("truenorth.worker.detection")


def detection_scorer(exercise_id: str, session: Callable, backend: str) -> Any | None:
    """Always None: the worker never credits a detection (ADR 0005).

    A deployment that still sets ``DETECTION_SCORING=on`` is told so once per run, rather
    than silently believing its exercises are scored from telemetry.
    """
    if backend != "mock" and os.getenv("DETECTION_SCORING", "off").strip().lower() in ("1", "on", "true", "yes"):
        logger.warning(
            "[detection] DETECTION_SCORING is retired (ADR 0005); exercise %s earns credit only from "
            "Student detection submissions",
            exercise_id,
        )
    return None
