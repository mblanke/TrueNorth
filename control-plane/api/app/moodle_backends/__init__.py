"""Moodle course-publishing backends (ADR 0001).

Usage:
    from app.moodle_backends import get_moodle_backend

    backend = get_moodle_backend(platform.platform_type, key_provider=...)
    backend.upsert_course(platform, payload)

The registry is keyed by ``external_platforms.platform_type``. "moodle" is a Moodle with
the local_truenorth plugin; "fake" is the in-memory stand-in tests and offline
development use. Adding a Moodle flavour = one module here and one registry entry.
"""

from __future__ import annotations

from typing import Any

from .base import BaseMoodleBackend, MoodleError
from .fake import FakeMoodle
from .local_truenorth import KeyProvider, LocalTrueNorthMoodle

__all__ = [
    "BaseMoodleBackend",
    "FakeMoodle",
    "LocalTrueNorthMoodle",
    "MoodleError",
    "get_moodle_backend",
    "supported_moodle_types",
]

_REGISTRY: dict[str, type[BaseMoodleBackend]] = {
    LocalTrueNorthMoodle.kind: LocalTrueNorthMoodle,
    FakeMoodle.kind: FakeMoodle,
}


def get_moodle_backend(kind: str | None, key_provider: KeyProvider | None = None, **kwargs: Any) -> BaseMoodleBackend:
    """The publishing backend for a platform type; ValueError if it cannot publish courses."""
    cls = _REGISTRY.get(kind or "")
    if cls is None:
        raise ValueError(
            f"platform type {kind!r} cannot receive course publications; one of {supported_moodle_types()}"
        )
    return cls(key_provider=key_provider, **kwargs)


def supported_moodle_types() -> list[str]:
    return sorted(_REGISTRY)
