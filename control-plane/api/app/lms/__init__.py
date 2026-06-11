"""TrueNorth Range — LMS backend registry and factory.

Usage:
    from app.lms import get_lms_backend

    backend = get_lms_backend()
    ok = await backend.emit_statement(stmt)

Configuration:
    LMS_BACKEND env var selects the active backend (default: xapi_lrs).

    Supported values:
        xapi_lrs  — xAPI 1.0.3-compliant LRS (yetanalytics, OpenDash, Ralph…)
        null      — No-op, swallows all statements (useful for offline/test)

Adding a new backend:
    1. Create control-plane/api/app/lms/<name>.py implementing BaseLMSBackend
    2. Add an entry to _REGISTRY below
    3. Set LMS_BACKEND=<name> in the environment — no other changes required
"""

from __future__ import annotations

import os

from .base import BaseLMSBackend
from .null import NullLMSBackend
from .xapi_lrs import XAPILRSBackend

__all__ = [
    "BaseLMSBackend",
    "NullLMSBackend",
    "XAPILRSBackend",
    "get_lms_backend",
]

_REGISTRY: dict[str, type[BaseLMSBackend]] = {
    "xapi_lrs": XAPILRSBackend,
    "null": NullLMSBackend,
}

_instance: BaseLMSBackend | None = None


def get_lms_backend() -> BaseLMSBackend:
    """Return the singleton LMS backend instance.

    Reads LMS_BACKEND on first call and caches the result.  Thread-safe
    for typical async usage patterns (single event-loop startup).

    Raises ValueError for unknown backend names to catch misconfiguration
    early at process startup rather than silently swallowing statements.
    """
    global _instance
    if _instance is None:
        name = os.getenv("LMS_BACKEND", "xapi_lrs").lower().strip()
        cls = _REGISTRY.get(name)
        if cls is None:
            valid = ", ".join(sorted(_REGISTRY))
            raise ValueError(
                f"Unknown LMS_BACKEND={name!r}. Valid options: {valid}"
            )
        _instance = cls()
    return _instance


def _reset_backend() -> None:  # pragma: no cover — test helper only
    """Force re-initialisation of the backend singleton.

    Call this in tests that need to switch backends between test cases.
    Do not use in production code.
    """
    global _instance
    _instance = None
